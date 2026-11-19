"""Offline inspection of recorded DICOM associations.

This module provides a self-contained model for auditing previously captured
DICOM network traffic. It does not use the live
:class:`~pynetdicom.association.Association` machinery; the captured
A-ASSOCIATE and P-DATA PDUs are decoded and audited as they were delivered.

The main entry point is :func:`inspect_recording`, which returns a
:class:`Recording` containing one :class:`AssociationRecord` per association.
Each record exposes a :class:`ContextView` - the presentation contexts that
were actually negotiated for that association - and a list of
:class:`MessageRecord` items with a per-message :class:`MessageStatus`.

The auditing rules follow the negotiated context strictly:

* Presentation contexts are looked up by their context ID within the
  association the message was sent on. Contexts from other associations are
  never used to fill in missing information.
* Only transfer syntaxes accepted by the A-ASSOCIATE response are used.
  Transfer syntaxes that were merely proposed are not treated as accepted.
* The transfer syntax used to decode a data set is the one accepted for the
  context the message was sent under.
* A data set is only considered complete when the final PDV fragment and the
  encoded data set are both present and fully consumed.

Each message is reported as one of:

``explainable``
    The command and data set are consistent with the negotiated context.

``encoding_conflict``
    The command or data set contradicts the negotiated context, such as a
    SOP Class mismatch or a data set that is not encoded using the accepted
    transfer syntax. A conflict is reported even if a lenient decoder happens
    to read plausible values from the data set.

``insufficient_evidence``
    The recording does not contain enough information to verify the message,
    such as a message sent under a context that was not accepted, a sender
    acting in a role that was not negotiated, or a data set whose end was not
    captured.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydicom.dataelem import RawDataElement
from pydicom.dataset import Dataset
from pydicom.tag import BaseTag
from pydicom.uid import UID
from pydicom.valuerep import VR

from pynetdicom.dimse_messages import DIMSEMessage
from pynetdicom.pdu import (
    A_ABORT_RQ,
    A_ASSOCIATE_AC,
    A_ASSOCIATE_RJ,
    A_ASSOCIATE_RQ,
    A_RELEASE_RP,
    A_RELEASE_RQ,
    P_DATA_TF,
)
from pynetdicom.pdu_primitives import P_DATA
from pynetdicom.presentation import (
    CONTEXT_REJECTED,
    DEFAULT_ROLE,
    SCP_SCU_ROLES,
    PresentationContext,
)

if TYPE_CHECKING:  # pragma: no cover
    from pynetdicom.pdu import PDU


LOGGER = logging.getLogger(__name__)

__all__ = [
    "AssociationRecord",
    "ContextView",
    "Direction",
    "IssueCode",
    "MessageRecord",
    "MessageStatus",
    "Recording",
    "RecordingError",
    "Role",
    "SOPClassContext",
    "build_context_view",
    "inspect_recording",
    "read_recording",
]


_NO_VALUE_LENGTH = 0xFFFFFFFF
_EXPLICIT_VR_LENGTH_32 = {
    "OB",
    "OD",
    "OF",
    "OL",
    "OV",
    "OW",
    "SQ",
    "SV",
    "UC",
    "UN",
    "UR",
    "UT",
    "UV",
}
_VALID_VR_CODES = {vr.value for vr in VR if " " not in vr.value}
_ITEM_TAGS = {0xFFFEE000, 0xFFFEE00D, 0xFFFEE0DD}
_FILE_META_TRANSFER_SYNTAX = BaseTag(0x00020010)


class RecordingError(ValueError):
    """Raised when a recording cannot be decoded."""


class _EncodingError(ValueError):
    """Raised when a data set does not match its declared encoding."""


class _UnknownSyntax(ValueError):
    """Raised when a transfer syntax has unknown encoding properties."""


class Role(str, Enum):
    """The roles an application entity may take in a presentation context."""

    SCU = "scu"
    """Service Class User."""

    SCP = "scp"
    """Service Class Provider."""


class Direction(str, Enum):
    """The side of an association a recorded PDU came from."""

    REQUESTOR = "requestor"
    """The association requestor (the calling application entity)."""

    ACCEPTOR = "acceptor"
    """The association acceptor (the called application entity)."""


class MessageStatus(str, Enum):
    """The audit status of a recorded DIMSE message."""

    EXPLAINABLE = "explainable"
    """The message is consistent with the negotiated context."""

    ENCODING_CONFLICT = "encoding_conflict"
    """The message contradicts the negotiated context's encoding."""

    ENCODING_CONTRADICTION = "encoding_conflict"  # noqa: PIE796
    """Alias for :attr:`ENCODING_CONFLICT`."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """There is not enough evidence to decide whether the message is valid."""

    @classmethod
    def _missing_(cls, value: object) -> "MessageStatus | None":
        """Return a status for leniently spelled values."""
        if not isinstance(value, str):
            return None

        text = value.strip().lower().replace("_", " ").replace("-", " ")
        normalised = text.replace(" ", "_")
        synonyms = {
            "encoding contradiction": "encoding_conflict",
            "conflict": "encoding_conflict",
            "contradiction": "encoding_conflict",
            "insufficient evidence": "insufficient_evidence",
            "explained": "explainable",
            "ok": "explainable",
            "valid": "explainable",
        }
        normalised = synonyms.get(text.replace("_", " "), normalised)
        for member in cls:
            if member.value == normalised or member.name.lower() == normalised:
                return member

        return None

    def __str__(self) -> str:
        """Return the status value."""
        return self.value


class IssueCode(str, Enum):
    """Machine readable explanations for a message's audit status."""

    UNACCEPTED_CONTEXT = "unaccepted_context"
    """The presentation context was proposed but not accepted."""

    UNKNOWN_CONTEXT = "unknown_context"
    """The presentation context does not belong to this association."""

    CONTEXT_MISMATCH = "context_mismatch"
    """The command set and data set were delivered under different contexts."""

    ROLE_NOT_ALLOWED = "role_not_allowed"
    """The sender's role was not negotiated for the context."""

    SOP_CLASS_CONFLICT = "sop_class_conflict"
    """The command or data set SOP Class differs from the context."""

    SOP_INSTANCE_CONFLICT = "sop_instance_conflict"
    """The command and data set SOP Instance UIDs differ."""

    TRANSFER_SYNTAX_CONFLICT = "transfer_syntax_conflict"
    """The declared transfer syntax differs from the accepted one."""

    DATA_SET_ENCODING_CONFLICT = "data_set_encoding_conflict"
    """The data set is not encoded using the accepted transfer syntax."""

    UNKNOWN_TRANSFER_SYNTAX = "unknown_transfer_syntax"
    """The accepted transfer syntax has unknown encoding properties."""

    FILE_META_ENCODING_CONFLICT = "file_meta_encoding_conflict"
    """The data set's File Meta Information does not match the context."""

    MISSING_DATA_SET_END = "missing_data_set_end"
    """The final data set fragment was not captured."""

    MISSING_DATA_SET = "missing_data_set"
    """The command declares a data set that was not captured."""

    MISSING_COMMAND_SET_END = "missing_command_set_end"
    """The final command set fragment was not captured."""

    INVALID_MESSAGE = "invalid_message"
    """The DIMSE message could not be decoded."""

    @classmethod
    def _missing_(cls, value: object) -> "IssueCode | None":
        """Return an issue code for leniently spelled values."""
        if not isinstance(value, str):
            return None

        text = value.strip().lower().replace("_", " ").replace("-", " ")
        normalised = text.replace(" ", "_")
        synonyms = {
            "not accepted": "unaccepted_context",
            "rejected context": "unaccepted_context",
            "role forbidden": "role_not_allowed",
            "role not permitted": "role_not_allowed",
            "sop class mismatch": "sop_class_conflict",
            "encoding contradiction": "data_set_encoding_conflict",
            "transfer syntax mismatch": "transfer_syntax_conflict",
            "truncated data set": "missing_data_set_end",
            "missing data": "missing_data_set",
        }
        normalised = synonyms.get(text.replace("_", " "), normalised)
        for member in cls:
            if member.value == normalised or member.name.lower() == normalised:
                return member

        return None


@dataclass(frozen=True)
class SOPClassContext:
    """A presentation context that was accepted for an association.

    Attributes
    ----------
    context_id : int
        The presentation context ID.
    abstract_syntax : pydicom.uid.UID
        The accepted SOP Class.
    transfer_syntax : pydicom.uid.UID
        The transfer syntax accepted for this context.
    requestor_roles : tuple of Role
        The roles the association requestor may take for this context.
    acceptor_roles : tuple of Role
        The roles the association acceptor may take for this context.
    """

    context_id: int
    abstract_syntax: UID
    transfer_syntax: UID
    requestor_roles: tuple[Role, ...] = (Role.SCU,)
    acceptor_roles: tuple[Role, ...] = (Role.SCP,)

    @property
    def sop_class(self) -> UID:
        """Return the accepted SOP Class UID."""
        return self.abstract_syntax

    @property
    def sop_class_uid(self) -> UID:
        """Return the accepted SOP Class UID."""
        return self.abstract_syntax

    @property
    def transfer_syntax_uid(self) -> UID:
        """Return the accepted transfer syntax UID."""
        return self.transfer_syntax

    @property
    def requestor_scu(self) -> bool:
        """Return ``True`` if the requestor may act as SCU."""
        return Role.SCU in self.requestor_roles

    @property
    def requestor_scp(self) -> bool:
        """Return ``True`` if the requestor may act as SCP."""
        return Role.SCP in self.requestor_roles

    @property
    def acceptor_scu(self) -> bool:
        """Return ``True`` if the acceptor may act as SCU."""
        return Role.SCU in self.acceptor_roles

    @property
    def acceptor_scp(self) -> bool:
        """Return ``True`` if the acceptor may act as SCP."""
        return Role.SCP in self.acceptor_roles

    def roles_for(self, direction: Direction | str) -> tuple[Role, ...]:
        """Return the roles allowed for `direction`."""
        direction = _normalise_direction(direction)
        if direction is Direction.REQUESTOR:
            return self.requestor_roles
        if direction is Direction.ACCEPTOR:
            return self.acceptor_roles

        raise ValueError(f"Unknown direction '{direction}'")

    def allows(self, direction: Direction | str, role: Role | str) -> bool:
        """Return ``True`` if `role` is allowed for `direction`."""
        return Role(role) in self.roles_for(direction)


@dataclass(frozen=True)
class ContextView:
    """The presentation contexts negotiated for a single association.

    A view is bound to one association. It does not fall back to contexts
    from other associations, and it does not treat proposed transfer syntaxes
    as accepted ones.

    Attributes
    ----------
    association_id : int
        The index of the association within the recording.
    contexts : tuple of SOPClassContext
        The accepted presentation contexts.
    rejected : tuple of PresentationContext
        The presentation contexts that were proposed but not accepted.
    proposed : tuple of PresentationContext
        All presentation contexts proposed by the association requestor.
    """

    association_id: int
    contexts: tuple[SOPClassContext, ...] = ()
    rejected: tuple[PresentationContext, ...] = ()
    proposed: tuple[PresentationContext, ...] = ()
    negotiated: bool = False

    def __iter__(self) -> Iterator[SOPClassContext]:
        """Iterate over the accepted contexts."""
        return iter(self.contexts)

    def __len__(self) -> int:
        """Return the number of accepted contexts."""
        return len(self.contexts)

    def __getitem__(self, index: int) -> SOPClassContext:
        """Return the accepted context for a context ID or list index."""
        context = self.by_id(index)
        if context is not None:
            return context

        return self.contexts[index]

    def keys(self) -> tuple[int, ...]:
        """Return the accepted context IDs."""
        return self.context_ids

    def values(self) -> tuple[SOPClassContext, ...]:
        """Return the accepted contexts."""
        return self.contexts

    def items(self) -> tuple[tuple[int, SOPClassContext], ...]:
        """Return the accepted ``(context ID, context)`` pairs."""
        return tuple((cx.context_id, cx) for cx in self.contexts)

    @property
    def accepted(self) -> tuple[SOPClassContext, ...]:
        """Return the accepted contexts."""
        return self.contexts

    @property
    def accepted_contexts(self) -> tuple[SOPClassContext, ...]:
        """Return the accepted contexts."""
        return self.contexts

    @property
    def rejected_contexts(self) -> tuple[PresentationContext, ...]:
        """Return the rejected contexts."""
        return self.rejected

    @property
    def context_ids(self) -> tuple[int, ...]:
        """Return the accepted context IDs."""
        return tuple(cx.context_id for cx in self.contexts)

    @property
    def sop_class_uids(self) -> tuple[UID, ...]:
        """Return the accepted SOP Class UIDs, in context ID order."""
        return tuple(cx.abstract_syntax for cx in self.contexts)

    def by_id(self, context_id: int) -> SOPClassContext | None:
        """Return the accepted context with `context_id`, if any."""
        for context in self.contexts:
            if context.context_id == context_id:
                return context

        return None

    get = by_id

    def by_sop_class(self, sop_class_uid: str | UID) -> tuple[SOPClassContext, ...]:
        """Return the accepted contexts for `sop_class_uid`."""
        uid = UID(sop_class_uid)
        return tuple(cx for cx in self.contexts if cx.abstract_syntax == uid)

    def for_sop_class(self, sop_class_uid: str | UID) -> tuple[SOPClassContext, ...]:
        """Return the accepted contexts for `sop_class_uid`."""
        return self.by_sop_class(sop_class_uid)

    def roles_for(self, context_id: int) -> tuple[Role, ...] | None:
        """Return the requestor roles allowed for `context_id`."""
        context = self.by_id(context_id)
        if context is None:
            return None

        return context.requestor_roles

    def is_accepted(self, context_id: int) -> bool:
        """Return ``True`` if `context_id` was accepted."""
        return self.by_id(context_id) is not None

    def is_rejected(self, context_id: int) -> bool:
        """Return ``True`` if `context_id` was proposed but not accepted."""
        return any(cx.context_id == context_id for cx in self.rejected)

    def proposed_syntaxes(self, sop_class_uid: str | UID) -> tuple[UID, ...]:
        """Return all transfer syntaxes proposed for `sop_class_uid`."""
        uid = UID(sop_class_uid)
        syntaxes: list[UID] = []
        for context in self.proposed:
            if context.abstract_syntax != uid:
                continue
            for syntax in context.transfer_syntax:
                if syntax not in syntaxes:
                    syntaxes.append(syntax)

        return tuple(syntaxes)


@dataclass
class MessageRecord:
    """A single DIMSE message found in a recording.

    Attributes
    ----------
    association_id : int
        The association the message was sent on.
    context_id : int
        The presentation context ID the message was sent under.
    message : pynetdicom.dimse_messages.DIMSEMessage
        The decoded DIMSE message.
    status : MessageStatus
        The audit result.
    issues : tuple of str
        Human readable explanations for the status.
    issue_codes : tuple of IssueCode
        Machine readable explanations for the status.
    """

    association_id: int
    context_id: int
    message: DIMSEMessage
    status: MessageStatus = MessageStatus.EXPLAINABLE
    issues: tuple[str, ...] = ()
    issue_codes: tuple[IssueCode, ...] = ()
    sender: Direction | None = None
    sender_role: Role | None = None
    declared_transfer_syntax: UID | None = None
    data_set: Dataset | None = None
    context: SOPClassContext | None = None

    @property
    def command_set(self) -> Dataset:
        """Return the message's command set."""
        return self.message.command_set

    @property
    def explanation(self) -> str:
        """Return the message's issues as a single string."""
        return "; ".join(self.issues)

    @property
    def dataset(self) -> Dataset | None:
        """Return the decoded data set, if available."""
        return self.data_set

    @property
    def encoded_data_set(self) -> bytes | None:
        """Return the raw encoded data set, if present."""
        data = self.message.data_set
        if data is None:
            return None

        value = data.getvalue()
        return value or None

    @property
    def is_request(self) -> bool:
        """Return ``True`` if the message is a service request."""
        return self.message.__class__.__name__.endswith("_RQ")

    @property
    def is_response(self) -> bool:
        """Return ``True`` if the message is a service response."""
        return self.message.__class__.__name__.endswith("_RSP")

    @property
    def message_type(self) -> str:
        """Return the DIMSE message type, e.g. ``"C-STORE-RQ"``."""
        return self.message.__class__.__name__.replace("_", "-")

    @property
    def deliverable(self) -> bool:
        """Return ``True`` if the message has been fully verified."""
        return self.status is MessageStatus.EXPLAINABLE

    @property
    def verified(self) -> bool:
        """Return ``True`` if the message has been fully verified."""
        return self.deliverable

    @property
    def explainable(self) -> bool:
        """Return ``True`` if the message is explainable."""
        return self.status is MessageStatus.EXPLAINABLE

    def has_issue(self, code: IssueCode | str) -> bool:
        """Return ``True`` if the message has the issue `code`."""
        code = IssueCode(code)
        return code in self.issue_codes

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation of the message."""
        return {
            "association_id": self.association_id,
            "context_id": self.context_id,
            "message_type": self.message_type,
            "status": self.status.value,
            "issues": list(self.issues),
            "issue_codes": [code.value for code in self.issue_codes],
            "sender": self.sender.value if self.sender else None,
            "sender_role": self.sender_role.value if self.sender_role else None,
            "declared_transfer_syntax": (
                str(self.declared_transfer_syntax)
                if self.declared_transfer_syntax
                else None
            ),
        }


@dataclass
class AssociationRecord:
    """A recorded association and the audit results for its messages.

    Attributes
    ----------
    association_id : int
        The index of the association within the recording.
    request : pynetdicom.pdu.A_ASSOCIATE_RQ
        The association request.
    response : A-ASSOCIATE-AC or A-ASSOCIATE-RJ or None
        The association response, if captured.
    contexts : ContextView
        The negotiated presentation contexts.
    messages : list of MessageRecord
        The DIMSE messages sent on this association.
    """

    association_id: int
    request: A_ASSOCIATE_RQ | None
    response: A_ASSOCIATE_AC | A_ASSOCIATE_RJ | None
    contexts: ContextView
    messages: list[MessageRecord] = field(default_factory=list)

    @property
    def context_view(self) -> ContextView:
        """Return the association's context view."""
        return self.contexts

    @property
    def accepted_contexts(self) -> tuple[SOPClassContext, ...]:
        """Return the association's accepted contexts."""
        return self.contexts.contexts

    @property
    def is_accepted(self) -> bool:
        """Return ``True`` if the association was accepted."""
        return isinstance(self.response, A_ASSOCIATE_AC)

    @property
    def is_rejected(self) -> bool:
        """Return ``True`` if the association was rejected."""
        return isinstance(self.response, A_ASSOCIATE_RJ)

    @property
    def calling_ae_title(self) -> str | None:
        """Return the requestor's AE title, if available."""
        if self.request is None:
            return None

        return self.request.calling_ae_title

    @property
    def called_ae_title(self) -> str | None:
        """Return the acceptor's AE title, if available."""
        if self.request is None:
            return None

        return self.request.called_ae_title

    @property
    def deliverables(self) -> list[MessageRecord]:
        """Return the messages that have been fully verified."""
        return [msg for msg in self.messages if msg.deliverable]

    @property
    def problems(self) -> list[MessageRecord]:
        """Return the messages that are not fully verified."""
        return [msg for msg in self.messages if not msg.deliverable]

    def context_for(self, context_id: int) -> SOPClassContext | None:
        """Return the accepted context for `context_id`."""
        return self.contexts.by_id(context_id)

    def deliver(self, *, datasets: bool = False) -> list[MessageRecord] | list[Dataset]:
        """Return the messages that have been fully verified.

        Parameters
        ----------
        datasets : bool, optional
            If ``False`` (default) then return the verified messages. If
            ``True`` then return the verified decoded data sets.

        Returns
        -------
        list of MessageRecord or list of pydicom.dataset.Dataset
            The verified messages, or their data sets if `datasets` is
            ``True``. Messages with an ``encoding_conflict`` or
            ``insufficient_evidence`` status are omitted.
        """
        messages = self.deliverables
        if datasets:
            return [
                message.data_set for message in messages if message.data_set is not None
            ]

        return messages

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation of the association."""
        return {
            "association_id": self.association_id,
            "calling_ae_title": self.calling_ae_title,
            "called_ae_title": self.called_ae_title,
            "is_accepted": self.is_accepted,
            "contexts": [
                {
                    "context_id": cx.context_id,
                    "abstract_syntax": str(cx.abstract_syntax),
                    "transfer_syntax": str(cx.transfer_syntax),
                    "requestor_roles": [role.value for role in cx.requestor_roles],
                    "acceptor_roles": [role.value for role in cx.acceptor_roles],
                }
                for cx in self.contexts.contexts
            ],
            "rejected_context_ids": [cx.context_id for cx in self.contexts.rejected],
            "messages": [msg.to_dict() for msg in self.messages],
        }


class Recording(list):
    """A list of :class:`AssociationRecord` items with convenience helpers."""

    @property
    def associations(self) -> "Recording":
        """Return the recorded associations."""
        return self

    @property
    def context_views(self) -> list[ContextView]:
        """Return the context view for each association."""
        return [record.contexts for record in self]

    def context_view(self, association_id: int = 0) -> ContextView:
        """Return the context view for the association at `association_id`."""
        return cast(AssociationRecord, self[association_id]).contexts

    def association(self, association_id: int = 0) -> AssociationRecord:
        """Return the association at `association_id`."""
        return cast(AssociationRecord, self[association_id])

    @property
    def messages(self) -> list[MessageRecord]:
        """Return every message in the recording."""
        return [msg for record in self for msg in record.messages]

    @property
    def deliverables(self) -> list[MessageRecord]:
        """Return every fully verified message in the recording."""
        return [msg for msg in self.messages if msg.deliverable]

    @property
    def verified(self) -> list[MessageRecord]:
        """Return every fully verified message in the recording."""
        return self.deliverables

    def deliver(self, *, datasets: bool = False) -> list[MessageRecord] | list[Dataset]:
        """Return the messages that have been fully verified.

        Messages with an ``encoding_conflict`` or ``insufficient_evidence``
        status are omitted, regardless of whether their decoded fields happen
        to look plausible.
        """
        messages = self.deliverables
        if datasets:
            return [
                message.data_set for message in messages if message.data_set is not None
            ]

        return messages

    @property
    def problems(self) -> list[MessageRecord]:
        """Return every message that is not fully verified."""
        return [msg for msg in self.messages if not msg.deliverable]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation of the recording."""
        return {"associations": [record.to_dict() for record in self]}

    def to_json(self, **kwargs: Any) -> str:
        """Return the recording as a JSON string."""
        return json.dumps(self.to_dict(), **kwargs)


def _trim_pdata(data: bytes) -> bytes:
    """Return a P-DATA PDU with any complete PDV items and a valid length."""
    offset = 6
    end = len(data)
    items = bytearray()
    while offset + 4 <= end:
        (item_length,) = struct.unpack(">I", data[offset : offset + 4])
        item_end = offset + 4 + item_length
        if item_end > end:
            break

        items.extend(data[offset:item_end])
        offset = item_end

    if not items:
        return b""

    return data[:4] + struct.pack(">I", len(items)) + bytes(items)


def _decode_recording_bytes(
    data: bytes,
    *,
    tolerate_truncated: bool = False,
    truncated: list[bool] | None = None,
) -> list["PDU"]:
    """Decode a binary or hex text recording."""
    if not data:
        return []

    # A hex dump normally starts with whitespace, '0x' or a PDU type in hex;
    # a binary recording starts with the PDU type in the range 1-7.
    if data[0] in range(0x01, 0x08):
        return [
            _decode_pdu(chunk)
            for chunk in _split_pdus(
                data, tolerate_truncated=tolerate_truncated, truncated=truncated
            )
        ]

    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return [
            _decode_pdu(chunk)
            for chunk in _split_pdus(
                data, tolerate_truncated=tolerate_truncated, truncated=truncated
            )
        ]

    compact = "".join(text.split())
    if compact.startswith("0x"):
        compact = compact[2:]
    if compact and len(compact) % 2 == 0:
        try:
            raw = bytes.fromhex(compact)
        except ValueError:
            raw = None

        if raw:
            return [
                _decode_pdu(chunk)
                for chunk in _split_pdus(
                    raw,
                    tolerate_truncated=tolerate_truncated,
                    truncated=truncated,
                )
            ]

    return [
        _decode_pdu(chunk)
        for chunk in _split_pdus(
            data, tolerate_truncated=tolerate_truncated, truncated=truncated
        )
    ]


def _split_pdus(
    data: bytes,
    *,
    tolerate_truncated: bool = False,
    truncated: list[bool] | None = None,
) -> Iterator[bytes]:
    """Yield the encoded PDUs contained in `data`."""
    offset = 0
    length = len(data)
    while offset < length:
        if offset + 6 > length:
            if tolerate_truncated:
                if truncated is not None:
                    truncated[0] = True
                return
            raise RecordingError(
                "Incomplete PDU header at byte "
                f"{offset} ({length - offset} bytes remaining)"
            )

        pdu_type, _, pdu_length = struct.unpack(">BBL", data[offset : offset + 6])
        end = offset + 6 + pdu_length
        if end > length:
            if tolerate_truncated:
                if truncated is not None:
                    truncated[0] = True
                if pdu_type == 0x04:
                    trimmed = _trim_pdata(data[offset:length])
                    if trimmed:
                        yield trimmed
                return
            raise RecordingError(
                f"Incomplete 0x{pdu_type:02X} PDU at byte {offset}: expected "
                f"{6 + pdu_length} bytes, found {length - offset}"
            )

        yield data[offset:end]
        offset = end


def _decode_pdu(bytestream: bytes | bytearray | memoryview) -> "PDU":
    """Return the decoded PDU for `bytestream`."""
    data = bytes(bytestream)
    if not data:
        raise RecordingError("Cannot decode an empty PDU")

    classes = {
        0x01: A_ASSOCIATE_RQ,
        0x02: A_ASSOCIATE_AC,
        0x03: A_ASSOCIATE_RJ,
        0x04: P_DATA_TF,
        0x05: A_RELEASE_RQ,
        0x06: A_RELEASE_RP,
        0x07: A_ABORT_RQ,
    }
    try:
        cls = classes[data[0]]
    except KeyError:
        raise RecordingError(f"Unsupported PDU type 0x{data[0]:02X}") from None

    pdu = cls()
    pdu.decode(data)

    return cast("PDU", pdu)


def _read_source(source: Any) -> bytes:
    """Return the bytestring for a path-like or file-like `source`."""
    if isinstance(source, (str, Path, os.PathLike)):
        with open(source, "rb") as file:
            return file.read()

    if hasattr(source, "read"):
        return cast(bytes, source.read())

    raise TypeError(f"Unsupported recording source type '{type(source)}'")


def _normalise_direction(value: object) -> Direction:
    """Return the :class:`Direction` for `value`."""
    if isinstance(value, Direction):
        return value

    if isinstance(value, Role):
        return Direction.REQUESTOR if value is Role.SCU else Direction.ACCEPTOR

    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("requestor", "request", "rq", "calling", "scu", "sent", "send"):
            return Direction.REQUESTOR
        if text in (
            "acceptor",
            "accept",
            "ac",
            "called",
            "scp",
            "recv",
            "receive",
            "received",
        ):
            return Direction.ACCEPTOR
        if text in ("rsp", "response"):
            return Direction.ACCEPTOR

    raise ValueError(f"Unknown association direction '{value}'")


def _direction_from_item(item: object) -> Direction | None:
    """Return the direction encoded in a recording item, if any."""
    if isinstance(item, Mapping):
        for key in ("direction", "sender", "source", "from", "side"):
            if key in item:
                return _normalise_direction(item[key])

        return None

    if isinstance(item, tuple) and len(item) == 2:
        first, second = item
        if isinstance(first, (Direction, Role, str)) and not isinstance(
            first, (bytes, bytearray, memoryview)
        ):
            try:
                return _normalise_direction(first)
            except ValueError:
                return None

        if isinstance(second, (Direction, Role, str)) and not isinstance(
            second, (bytes, bytearray, memoryview)
        ):
            try:
                return _normalise_direction(second)
            except ValueError:
                return None

    return None


def _pdu_from_item(item: object) -> "PDU | None":
    """Return the PDU encoded in a recording item, if any."""
    if isinstance(item, tuple) and len(item) == 2:
        for candidate in item:
            if hasattr(candidate, "decode") and hasattr(candidate, "encode"):
                return cast("PDU", candidate)

        for candidate in item:
            if isinstance(candidate, (bytes, bytearray, memoryview)):
                return _decode_pdu(candidate)

        return None

    if isinstance(item, Mapping):
        for key in ("pdu", "encoded", "data", "bytes"):
            if key not in item:
                continue

            value = item[key]
            if hasattr(value, "decode") and hasattr(value, "encode"):
                return cast("PDU", value)
            if isinstance(value, (bytes, bytearray, memoryview)):
                return _decode_pdu(value)

        return None

    if hasattr(item, "decode") and hasattr(item, "encode"):
        return cast("PDU", item)

    if isinstance(item, (bytes, bytearray, memoryview)):
        return _decode_pdu(item)

    return None


def read_recording(source: Any) -> list["PDU"]:
    """Decode a recording into a list of PDUs.

    Parameters
    ----------
    source : bytes, path-like, file-like or iterable
        The recording to read. May be:

        * A bytestring containing one or more encoded PDUs.
        * A path to a file containing encoded PDUs.
        * A file-like object positioned at encoded PDUs.
        * An iterable of decoded PDU objects or encoded PDU bytestrings.

    Returns
    -------
    list of pynetdicom.pdu.PDU
        The decoded PDUs in capture order.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _decode_recording_bytes(bytes(source))

    if isinstance(source, (str, Path, os.PathLike)) or hasattr(source, "read"):
        return _decode_recording_bytes(_read_source(source))

    pdus: list[PDU] = []
    for item in cast(Iterable[Any], source):
        if isinstance(item, (bytes, bytearray, memoryview)):
            pdus.extend(_decode_recording_bytes(bytes(item)))
            continue

        pdu = _pdu_from_item(item)
        if pdu is not None:
            pdus.append(pdu)

    return pdus


def _iter_pdus(
    source: Any, *, truncated: list[bool] | None = None
) -> Iterator[tuple[Direction | None, "PDU"]]:
    """Yield ``(direction, pdu)`` for each PDU in `source`."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        for pdu in _decode_recording_bytes(
            bytes(source), tolerate_truncated=True, truncated=truncated
        ):
            yield None, pdu
        return

    if isinstance(source, (str, Path, os.PathLike)) or hasattr(source, "read"):
        for pdu in _decode_recording_bytes(
            _read_source(source), tolerate_truncated=True, truncated=truncated
        ):
            yield None, pdu
        return

    for item in cast(Iterable[Any], source):
        direction = _direction_from_item(item)
        if isinstance(item, (bytes, bytearray, memoryview)):
            for pdu in _decode_recording_bytes(
                bytes(item), tolerate_truncated=True, truncated=truncated
            ):
                yield direction, pdu
            continue

        decoded = _pdu_from_item(item)
        if decoded is not None:
            yield direction, decoded


def _role_bits(user_info: object) -> dict[UID, tuple[bool | None, bool | None]]:
    """Return the SCP/SCU role selection bits in `user_info`."""
    roles_getter = getattr(user_info, "role_selection", None)
    if roles_getter is None:
        return {}

    roles: dict[UID, tuple[bool | None, bool | None]] = {}
    for uid, item in roles_getter.items():
        roles[UID(uid)] = (bool(item.scu_role), bool(item.scp_role))

    return roles


def _negotiated_roles(
    request_bits: dict[UID, tuple[bool | None, bool | None]],
    response_bits: dict[UID, tuple[bool | None, bool | None]],
    abstract_syntax: UID,
) -> tuple[tuple[Role, ...], tuple[Role, ...]]:
    """Return the (requestor, acceptor) roles for an accepted context."""
    rq_roles = request_bits.get(abstract_syntax, (None, None))
    ac_roles = response_bits.get(abstract_syntax, (None, None))
    try:
        outcome = SCP_SCU_ROLES[rq_roles][ac_roles]
    except KeyError:
        outcome = DEFAULT_ROLE

    if outcome is CONTEXT_REJECTED:
        return (), ()

    requestor_roles = []
    if outcome[0]:
        requestor_roles.append(Role.SCU)
    if outcome[1]:
        requestor_roles.append(Role.SCP)

    acceptor_roles = []
    if outcome[2]:
        acceptor_roles.append(Role.SCU)
    if outcome[3]:
        acceptor_roles.append(Role.SCP)

    return tuple(requestor_roles), tuple(acceptor_roles)


def build_context_view(
    association_id: int,
    request: A_ASSOCIATE_RQ | None,
    response: A_ASSOCIATE_AC | A_ASSOCIATE_RJ | None,
) -> ContextView:
    """Return the negotiated context view for an association.

    Only contexts accepted by the A-ASSOCIATE response are included in the
    view's accepted contexts. Contexts from other associations are never
    considered.

    Parameters
    ----------
    association_id : int
        The index of the association within the recording.
    request : A-ASSOCIATE-RQ PDU or None
        The association request.
    response : A-ASSOCIATE-AC or A-ASSOCIATE-RJ PDU or None
        The association response.

    Returns
    -------
    ContextView
        The negotiated context view.
    """
    if request is None:
        return ContextView(association_id=association_id)

    proposed_contexts = {cx.context_id: cx for cx in request.presentation_context}
    proposed = tuple(cx.to_primitive() for cx in proposed_contexts.values())
    request_roles = _role_bits(request.user_information)

    if not isinstance(response, A_ASSOCIATE_AC):
        if isinstance(response, A_ASSOCIATE_RJ):
            return ContextView(
                association_id=association_id,
                proposed=proposed,
                rejected=proposed,
                negotiated=True,
            )

        # No A-ASSOCIATE response was captured, so acceptance is unknown
        return ContextView(
            association_id=association_id,
            proposed=proposed,
            negotiated=False,
        )

    response_roles = _role_bits(response.user_information)
    results = {cx.context_id: cx for cx in response.presentation_context}

    accepted: list[SOPClassContext] = []
    rejected: list[PresentationContext] = []
    for context_id, request_context in sorted(proposed_contexts.items()):
        response_context = results.get(context_id)
        abstract_syntax = request_context.abstract_syntax
        if abstract_syntax is None:
            continue

        result = 0x02
        accepted_syntax = None
        if response_context is not None:
            result = cast(int, response_context.result)
            accepted_syntax = response_context.transfer_syntax

        if result != 0x00 or accepted_syntax is None:
            context = request_context.to_primitive()
            context.result = result
            rejected.append(context)
            continue

        requestor_roles, acceptor_roles = _negotiated_roles(
            request_roles, response_roles, UID(abstract_syntax)
        )
        accepted.append(
            SOPClassContext(
                context_id=cast(int, context_id),
                abstract_syntax=UID(abstract_syntax),
                transfer_syntax=UID(accepted_syntax),
                requestor_roles=requestor_roles,
                acceptor_roles=acceptor_roles,
            )
        )

    return ContextView(
        association_id=association_id,
        contexts=tuple(accepted),
        rejected=tuple(rejected),
        proposed=proposed,
        negotiated=True,
    )


def _message_sop_class(message: DIMSEMessage) -> UID | None:
    """Return the SOP Class UID the message concerns, if known."""
    command = message.command_set
    for keyword in ("AffectedSOPClassUID", "RequestedSOPClassUID"):
        value = getattr(command, keyword, None)
        if value:
            return UID(value)

    return None


def _message_sop_instance(message: DIMSEMessage) -> UID | None:
    """Return the SOP Instance UID the message concerns, if known."""
    command = message.command_set
    for keyword in ("AffectedSOPInstanceUID", "RequestedSOPInstanceUID"):
        value = getattr(command, keyword, None)
        if value:
            return UID(value)

    return None


def _split_file_meta(
    bytestream: bytes,
) -> tuple[UID | None, bytes, dict[str, str]]:
    """Return the File Meta's transfer syntax, data set and declarations.

    If `bytestream` does not start with File Meta Information then ``None``,
    `bytestream` and an empty mapping are returned.
    """
    offset = 0
    if len(bytestream) >= 132 and bytestream[128:132] == b"DICM":
        offset = 132
    elif not bytestream.startswith(b"\x02\x00\x00\x00"):
        return None, bytestream, {}

    start = offset
    transfer_syntax: UID | None = None
    declarations: dict[str, str] = {}
    end = len(bytestream)
    while offset + 8 <= end:
        group, element = struct.unpack("<HH", bytestream[offset : offset + 4])
        if group != 0x0002:
            break

        try:
            vr = bytestream[offset + 4 : offset + 6].decode("ascii")
        except UnicodeDecodeError:
            raise _EncodingError(f"Invalid File Meta VR at byte {offset}") from None

        if vr in _EXPLICIT_VR_LENGTH_32:
            if offset + 12 > end:
                raise _EncodingError(f"Truncated File Meta element at byte {offset}")
            (length,) = struct.unpack("<I", bytestream[offset + 8 : offset + 12])
            value_offset = offset + 12
        else:
            if vr not in _VALID_VR_CODES:
                raise _EncodingError(f"Invalid File Meta VR '{vr}' at byte {offset}")
            (length,) = struct.unpack("<H", bytestream[offset + 6 : offset + 8])
            value_offset = offset + 8

        if length == _NO_VALUE_LENGTH:
            raise _EncodingError(f"Undefined length File Meta element at byte {offset}")

        value_end = value_offset + length
        if value_end > end:
            raise _EncodingError(f"Truncated File Meta element value at byte {offset}")

        tag = BaseTag(group << 16 | element)
        if tag == _FILE_META_TRANSFER_SYNTAX:
            value = bytestream[value_offset:value_end].rstrip(b"\x00")
            transfer_syntax = UID(value.decode("ascii"))
            declarations["TransferSyntaxUID"] = str(transfer_syntax)
        elif tag == BaseTag(0x00020002):
            value = bytestream[value_offset:value_end].rstrip(b"\x00")
            declarations["MediaStorageSOPClassUID"] = value.decode("ascii")
        elif tag == BaseTag(0x00020003):
            value = bytestream[value_offset:value_end].rstrip(b"\x00")
            declarations["MediaStorageSOPInstanceUID"] = value.decode("ascii")

        offset = value_end

    if offset == start:
        if start:
            return transfer_syntax, bytestream[offset:], declarations

        return None, bytestream, {}

    return transfer_syntax, bytestream[offset:], declarations


def _is_known_transfer_syntax(syntax: UID) -> bool:
    """Return ``True`` if `syntax` has known encoding properties."""
    try:
        return bool(syntax.is_transfer_syntax)
    except Exception:
        return False


def _decode_data_set(bytestream: bytes, syntax: UID) -> Dataset:
    """Decode `bytestream` strictly using `syntax`.

    Raises
    ------
    _UnknownSyntax
        If the syntax has unknown encoding properties.
    _EncodingError
        If the data set is not a complete, valid encoding for `syntax`.
    """
    if not _is_known_transfer_syntax(syntax):
        raise _UnknownSyntax(f"Unknown transfer syntax '{syntax}'")

    data = bytestream
    if syntax.is_deflated:
        try:
            data = zlib.decompress(bytestream, -zlib.MAX_WBITS)
        except zlib.error as exc:
            raise _EncodingError(
                f"Data set is not valid deflated data: {exc}"
            ) from None

    # Compressed transfer syntaxes still use Explicit VR Little Endian for
    # the data set itself; only the pixel data is encapsulated. This lets us
    # verify the SOP Class, SOP Instance and structure without decoding pixel
    # data.
    is_implicit_vr = syntax.is_implicit_VR
    is_little_endian = syntax.is_little_endian
    raw_elements = {}
    for element in _read_elements(data, is_implicit_vr, is_little_endian):
        if element.tag in _ITEM_TAGS:
            raise _EncodingError(f"Unexpected item tag {element.tag}")

        raw_elements[element.tag] = element

    return Dataset(raw_elements)


def _read_elements(
    data: bytes, is_implicit_vr: bool, is_little_endian: bool
) -> Iterator[RawDataElement]:
    """Yield top-level :class:`~pydicom.dataelem.RawDataElement` from `data`.

    Unlike pydicom's default reader this does not attempt to detect or switch
    between implicit and explicit VR, and it rejects element lengths that run
    past the end of the data set.

    Sequences and encapsulated pixel data are validated structurally and
    skipped rather than decoded; the audit only needs the top-level elements
    such as the SOP Class and SOP Instance UIDs.
    """
    endian = "<" if is_little_endian else ">"
    offset = 0
    end = len(data)
    while offset < end:
        element, offset = _read_element(
            data, offset, end, endian, is_implicit_vr, is_little_endian
        )
        if element is not None:
            yield element


def _read_element(
    data: bytes,
    offset: int,
    end: int,
    endian: str,
    is_implicit_vr: bool,
    is_little_endian: bool,
) -> tuple[RawDataElement | None, int]:
    """Return the element at `offset` and the offset of the next element."""
    if offset + 8 > end:
        raise _EncodingError(
            f"Truncated data element header at byte {offset}: "
            f"{end - offset} byte(s) remaining"
        )

    group, element = struct.unpack(f"{endian}HH", data[offset : offset + 4])
    tag = BaseTag(group << 16 | element)
    if tag in _ITEM_TAGS:
        raise _EncodingError(f"Unexpected item tag {tag} at byte {offset}")

    if is_implicit_vr:
        (length,) = struct.unpack(f"{endian}I", data[offset + 4 : offset + 8])
        value_offset = offset + 8
        vr = None
    else:
        vr_bytes = data[offset + 4 : offset + 6]
        try:
            vr = vr_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise _EncodingError(
                f"Invalid VR {vr_bytes!r} for tag {tag} at byte {offset}"
            ) from None

        if vr not in _VALID_VR_CODES:
            raise _EncodingError(f"Invalid VR '{vr}' for tag {tag} at byte {offset}")

        if vr in _EXPLICIT_VR_LENGTH_32:
            if offset + 12 > end:
                raise _EncodingError(
                    f"Truncated data element header for tag {tag} at byte {offset}"
                )
            (length,) = struct.unpack(f"{endian}I", data[offset + 8 : offset + 12])
            value_offset = offset + 12
        else:
            (length,) = struct.unpack(f"{endian}H", data[offset + 6 : offset + 8])
            value_offset = offset + 8

    # DICOM sequences are skipped as a unit
    is_sequence = vr == "SQ"
    if is_sequence and length == _NO_VALUE_LENGTH:
        next_offset = _skip_undefined_length(
            data, value_offset, end, endian, is_implicit_vr, is_little_endian
        )
        return None, next_offset

    if length == _NO_VALUE_LENGTH:
        # Encapsulated pixel data and UN elements with undefined length use
        # the item structure; validate and skip.
        next_offset = _skip_undefined_length(
            data, value_offset, end, endian, is_implicit_vr, is_little_endian
        )
        return None, next_offset

    value_end = value_offset + length
    if value_end > end:
        raise _EncodingError(
            f"Data element {tag} at byte {offset} declares {length} value "
            f"bytes but only {end - value_offset} are available"
        )

    if is_sequence:
        return None, value_end

    value = data[value_offset:value_end]
    return (
        RawDataElement(
            tag,
            vr,
            length,
            value,
            value_offset,
            is_implicit_vr,
            is_little_endian,
        ),
        value_end,
    )


def _skip_undefined_length(
    data: bytes,
    offset: int,
    end: int,
    endian: str,
    is_implicit_vr: bool,
    is_little_endian: bool,
) -> int:
    """Validate and skip an undefined length sequence or encapsulated value."""
    while offset < end:
        if offset + 8 > end:
            raise _EncodingError(
                f"Truncated item header at byte {offset}: "
                f"{end - offset} byte(s) remaining"
            )

        group, element, length = struct.unpack(
            f"{endian}HHI", data[offset : offset + 8]
        )
        tag = group << 16 | element
        offset += 8
        if tag == 0xFFFEE0DD:
            return offset

        if tag != 0xFFFEE000:
            raise _EncodingError(
                f"Expected an item tag at byte {offset - 8}, found 0x{tag:08X}"
            )

        if length == _NO_VALUE_LENGTH:
            offset = _skip_undefined_item(
                data, offset, end, endian, is_implicit_vr, is_little_endian
            )
        else:
            offset += length

        if offset > end:
            raise _EncodingError(
                "Undefined length value runs past the end of the data set"
            )

    raise _EncodingError("Undefined length value is missing its sequence delimiter")


def _skip_undefined_item(
    data: bytes,
    offset: int,
    end: int,
    endian: str,
    is_implicit_vr: bool,
    is_little_endian: bool,
) -> int:
    """Validate and skip an undefined length item's dataset."""
    while offset < end:
        if offset + 8 > end:
            raise _EncodingError(
                f"Truncated item header at byte {offset}: "
                f"{end - offset} byte(s) remaining"
            )

        group, element = struct.unpack(f"{endian}HH", data[offset : offset + 4])
        tag = group << 16 | element
        if tag == 0xFFFEE00D:
            return offset + 8

        if tag == 0xFFFEE0DD:
            raise _EncodingError("Undefined length item is missing its item delimiter")

        _, offset = _read_element(
            data, offset, end, endian, is_implicit_vr, is_little_endian
        )

    raise _EncodingError("Undefined length item is missing its item delimiter")


@dataclass
class _AssembledMessage:
    """The state of a partially or fully assembled DIMSE message."""

    message: DIMSEMessage
    rules: "_AuditRules"
    context_id: int | None = None
    command_complete: bool = False
    data_expected: bool | None = None
    data_started: bool = False
    data_complete: bool = False
    sender: Direction | None = None
    seen: bool = False
    error: str | None = None
    context_mismatch: tuple[int, int] | None = None

    @property
    def complete(self) -> bool:
        """Return ``True`` if the message is fully assembled."""
        if not self.command_complete:
            return False

        if self.data_expected is False:
            return True

        return self.data_complete


@dataclass(frozen=True)
class _AuditRules:
    """Options controlling how messages are audited."""

    expected_role: Role | None = None


def _push_pdv(
    state: _AssembledMessage, context_id: int, value: bytes
) -> tuple["_AssembledMessage | None", "_AssembledMessage"]:
    """Push a PDV into `state`.

    Returns the previous state (if a new command set starts before the
    previous message's data set was completed) and the state to continue
    using, which may be a new object.
    """
    state.seen = True
    if state.context_id is None:
        state.context_id = context_id
    elif state.context_id != context_id:
        if state.context_mismatch is None:
            state.context_mismatch = (state.context_id, context_id)

    if not value:
        state.error = "Empty presentation data value"
        return None, state

    control = value[0]
    if control & 0xFC:
        state.error = (
            f"Reserved bits set in the PDV message control header 0x{control:02X}"
        )
        return None, state

    is_command = bool(control & 1)
    is_last = bool(control & 2)

    previous = None
    if is_command and state.command_complete and not state.complete:
        previous = state
        state = _AssembledMessage(DIMSEMessage(), state.rules, sender=state.sender)

    if state.error is not None:
        return None, state

    primitive = P_DATA()
    # P_DATA validates the runtime form ``[[context_id, value]]``
    primitive.presentation_data_value_list = [[context_id, value]]  # type: ignore[list-item]
    try:
        complete = state.message.decode_msg(primitive)
    except Exception as exc:
        state.error = str(exc)
        state.command_complete = True
        return previous, state

    if is_command:
        if is_last:
            state.command_complete = True
            state.context_id = context_id
            command_data_set = state.message.command_set.get("CommandDataSetType", None)
            state.data_expected = command_data_set != 0x0101
    else:
        state.data_started = True
        if is_last:
            state.data_complete = True

    # decode_msg() returns True for a final command set with no data set
    if complete and state.data_expected is False:
        state.command_complete = True

    return previous, state


def _infer_sender(
    message: DIMSEMessage,
    sender: Direction | None,
    context: SOPClassContext | None,
) -> tuple[Direction | None, Role | None]:
    """Return the (sender side, sender role) for `message`."""
    role = Role.SCU if message.__class__.__name__.endswith("_RQ") else Role.SCP
    if sender is not None:
        return sender, role

    if context is None:
        return None, role

    # Without captured direction evidence, infer the side from the roles that
    # were actually negotiated. If only one side may take the role then that
    # side must have sent the message; otherwise fall back to the conventional
    # direction (requests from the requestor, responses from the acceptor).
    candidates = []
    if role in context.requestor_roles:
        candidates.append(Direction.REQUESTOR)
    if role in context.acceptor_roles:
        candidates.append(Direction.ACCEPTOR)

    if len(candidates) == 1:
        return candidates[0], role

    if message.__class__.__name__.endswith("_RQ"):
        return Direction.REQUESTOR, role

    return Direction.ACCEPTOR, role


def _audit_message(
    state: _AssembledMessage,
    view: ContextView,
    *,
    sender: Direction | None,
    truncated: bool = False,
) -> MessageRecord:
    """Return a :class:`MessageRecord` for the assembled message."""
    issues: list[str] = []
    codes: list[IssueCode] = []
    status = MessageStatus.EXPLAINABLE

    def add(code: IssueCode, description: str, conflict: bool = False) -> None:
        codes.append(code)
        issues.append(description)
        nonlocal status
        if conflict:
            status = MessageStatus.ENCODING_CONFLICT
        elif status is MessageStatus.EXPLAINABLE:
            status = MessageStatus.INSUFFICIENT_EVIDENCE

    context_id = state.context_id if state.context_id is not None else -1
    context = view.by_id(context_id) if context_id >= 0 else None
    declared_transfer_syntax: UID | None = None
    dataset: Dataset | None = None
    sender_side: Direction | None = None
    sender_role: Role | None = None

    if state.context_mismatch is not None:
        command_context, data_context = state.context_mismatch
        add(
            IssueCode.CONTEXT_MISMATCH,
            f"The message starts under context ID {command_context} but "
            f"continues under context ID {data_context}",
            conflict=True,
        )

    # Context acceptance
    if context is None:
        if view.is_rejected(context_id):
            add(
                IssueCode.UNACCEPTED_CONTEXT,
                f"Presentation context ID {context_id} was proposed but not "
                "accepted for this association",
            )
        elif not view.negotiated:
            add(
                IssueCode.UNKNOWN_CONTEXT,
                f"The A-ASSOCIATE response was not captured, so context ID "
                f"{context_id} cannot be confirmed as accepted",
            )
        else:
            add(
                IssueCode.UNKNOWN_CONTEXT,
                f"Presentation context ID {context_id} does not belong to this "
                "association",
            )
    else:
        # Role
        sender_side, sender_role = _infer_sender(state.message, sender, context)
        if sender_side is not None and sender_role is not None:
            allowed = context.roles_for(sender_side)
            if sender_role not in allowed:
                add(
                    IssueCode.ROLE_NOT_ALLOWED,
                    f"The {sender_side.value} is not allowed to act as "
                    f"{sender_role.value.upper()} for context ID "
                    f"{context_id}",
                )
        else:
            sender_side = None
            sender_role = None

        if state.rules.expected_role is not None:
            if sender_role is None or sender_role is not state.rules.expected_role:
                add(
                    IssueCode.ROLE_NOT_ALLOWED,
                    f"The message was sent as {sender_role.value.upper() if sender_role else 'an unknown role'}"
                    f", expected {state.rules.expected_role.value.upper()}",
                )

        # SOP Class
        sop_class = _message_sop_class(state.message)
        if sop_class is not None and sop_class != context.abstract_syntax:
            add(
                IssueCode.SOP_CLASS_CONFLICT,
                f"Command set declares SOP Class '{sop_class}', but context ID "
                f"{context_id} was accepted for '{context.abstract_syntax}'",
                conflict=True,
            )

        # Data set evidence
        raw_data = state.message.data_set.getvalue() if state.message.data_set else b""
        if state.data_expected and not state.data_started:
            if truncated:
                add(
                    IssueCode.MISSING_DATA_SET_END,
                    "The recording ends before any data set fragments were "
                    "captured for this message",
                )
            else:
                add(
                    IssueCode.MISSING_DATA_SET,
                    "The command set declares a data set but no data set "
                    "fragments were captured for this message",
                )
        elif state.data_expected and not state.data_complete:
            add(
                IssueCode.MISSING_DATA_SET_END,
                "The command set declares a data set but the final data set "
                "fragment was not captured",
            )
        elif raw_data:
            try:
                declared_transfer_syntax, data, declarations = _split_file_meta(
                    raw_data
                )
                if (
                    declared_transfer_syntax is not None
                    and declared_transfer_syntax != context.transfer_syntax
                ):
                    add(
                        IssueCode.TRANSFER_SYNTAX_CONFLICT,
                        f"Data set declares transfer syntax "
                        f"'{declared_transfer_syntax.name}', but context ID "
                        f"{context_id} accepted "
                        f"'{context.transfer_syntax.name}'",
                        conflict=True,
                    )

                declared_sop_class = declarations.get("MediaStorageSOPClassUID")
                if (
                    declared_sop_class is not None
                    and UID(declared_sop_class) != context.abstract_syntax
                ):
                    add(
                        IssueCode.FILE_META_ENCODING_CONFLICT,
                        f"Data set File Meta declares SOP Class "
                        f"'{declared_sop_class}', but context ID {context_id} "
                        f"was accepted for '{context.abstract_syntax}'",
                        conflict=True,
                    )

                if declared_transfer_syntax is None or (
                    declared_transfer_syntax == context.transfer_syntax
                ):
                    dataset = _decode_data_set(data, context.transfer_syntax)
                    _check_data_set(
                        dataset,
                        state.message,
                        context,
                        add,
                    )
            except _UnknownSyntax as exc:
                add(
                    IssueCode.UNKNOWN_TRANSFER_SYNTAX,
                    f"The accepted transfer syntax cannot be interpreted "
                    f"offline: {exc}",
                )
            except _EncodingError as exc:
                detail = _describe_syntax_mismatch(raw_data, context, view)
                description = (
                    f"Data set could not be decoded using the accepted transfer "
                    f"syntax '{context.transfer_syntax.name}': {exc}"
                )
                if detail:
                    description += f"; {detail}"

                add(
                    IssueCode.DATA_SET_ENCODING_CONFLICT,
                    description,
                    conflict=True,
                )
            except Exception as exc:  # pragma: no cover - defensive
                add(
                    IssueCode.DATA_SET_ENCODING_CONFLICT,
                    f"Data set could not be decoded using the accepted transfer "
                    f"syntax '{context.transfer_syntax.name}': {exc}",
                    conflict=True,
                )

    if state.error is not None:
        add(
            IssueCode.INVALID_MESSAGE,
            f"The DIMSE message could not be decoded: {state.error}",
        )

    if not state.command_complete:
        add(
            IssueCode.MISSING_COMMAND_SET_END,
            "The final command set fragment was not captured",
        )

    return MessageRecord(
        association_id=-1,
        context_id=context_id,
        message=state.message,
        status=status,
        issues=tuple(issues),
        issue_codes=tuple(codes),
        sender=sender_side if context is not None else None,
        sender_role=sender_role if context is not None else None,
        declared_transfer_syntax=declared_transfer_syntax,
        data_set=dataset,
        context=context,
    )


def _describe_syntax_mismatch(
    raw_data: bytes, context: SOPClassContext, view: ContextView
) -> str | None:
    """Return a description of which proposed syntax the data resembles."""
    for syntax in view.proposed_syntaxes(context.abstract_syntax):
        if syntax == context.transfer_syntax or not _is_known_transfer_syntax(syntax):
            continue
        if syntax.is_compressed:
            continue

        try:
            data = raw_data
            if syntax.is_deflated:
                data = zlib.decompress(raw_data, -zlib.MAX_WBITS)
            dataset = _decode_data_set(data, syntax)
        except Exception:
            continue

        sop_class = dataset.get("SOPClassUID", None)
        if sop_class and UID(sop_class) != context.abstract_syntax:
            continue

        return (
            f"the data set is consistent with the proposed but not accepted "
            f"transfer syntax '{syntax.name}'"
        )

    return None


def _check_data_set(
    dataset: Dataset,
    message: DIMSEMessage,
    context: SOPClassContext,
    add: Any,
) -> None:
    """Check the decoded data set against the command set and context."""
    for keyword in ("SOPClassUID", "MediaStorageSOPClassUID"):
        value = dataset.get(keyword, None)
        if value and UID(value) != context.abstract_syntax:
            add(
                IssueCode.SOP_CLASS_CONFLICT,
                f"Data set declares '{keyword}' '{value}', but context ID "
                f"{context.context_id} was accepted for "
                f"'{context.abstract_syntax}'",
                conflict=True,
            )

    command_instance = _message_sop_instance(message)
    data_instance = dataset.get("SOPInstanceUID", None)
    if (
        command_instance
        and data_instance
        and UID(command_instance) != UID(data_instance)
    ):
        add(
            IssueCode.SOP_INSTANCE_CONFLICT,
            f"Command set declares SOP Instance '{command_instance}', but the "
            f"data set declares '{data_instance}'",
            conflict=True,
        )


def inspect_recording(
    source: Any,
    *,
    expected_role: Role | str | None = None,
) -> Recording:
    """Audit a recorded DICOM network exchange.

    The recording is divided into associations at each A-ASSOCIATE request.
    Each association gets its own :class:`ContextView` built from the
    A-ASSOCIATE request and response; contexts from other associations are
    never used.

    Parameters
    ----------
    source : bytes, path-like, file-like or iterable
        The recording to inspect. See :func:`read_recording` for the accepted
        forms. Items may optionally include a direction, either as a
        ``(direction, pdu)`` tuple or as a mapping with a ``"direction"`` key.
        Accepted direction values are ``"requestor"``, ``"acceptor"``,
        ``"sent"``, ``"recv"``, ``"scu"`` and ``"scp"``.
    expected_role : Role or str, optional
        If given, the role the sending application entity is expected to use
        for every message. Messages sent using another role are reported with
        an :attr:`~IssueCode.ROLE_NOT_ALLOWED` issue.

    Returns
    -------
    Recording
        The recorded associations, in capture order.
    """
    if expected_role is not None:
        expected_role = Role(expected_role)

    rules = _AuditRules(expected_role=expected_role)
    truncated = [False]
    pdus = list(_iter_pdus(source, truncated=truncated))

    groups: list[list[tuple[Direction | None, PDU]]] = []
    for direction, pdu in pdus:
        if isinstance(pdu, A_ASSOCIATE_RQ) or not groups:
            groups.append([])

        groups[-1].append((direction, pdu))

    recording = Recording()
    for association_id, group in enumerate(groups):
        request = next(
            (pdu for _, pdu in group if isinstance(pdu, A_ASSOCIATE_RQ)), None
        )
        response = next(
            (
                pdu
                for _, pdu in group
                if isinstance(pdu, (A_ASSOCIATE_AC, A_ASSOCIATE_RJ))
            ),
            None,
        )
        view = build_context_view(association_id, request, response)
        record = AssociationRecord(
            association_id=association_id,
            request=request,
            response=response,
            contexts=view,
        )

        states: dict[Direction | None, _AssembledMessage] = {}
        for direction, pdu in group:
            if not isinstance(pdu, P_DATA_TF):
                continue

            primitive = pdu.to_primitive()
            for pdv_context_id, pdv_value in primitive.presentation_data_value_list:
                if not pdv_value:
                    continue

                key = direction
                state = states.pop(key, None)
                if state is None:
                    state = _AssembledMessage(DIMSEMessage(), rules, sender=direction)

                previous, state = _push_pdv(state, pdv_context_id, pdv_value)
                if previous is not None:
                    message = _audit_message(previous, view, sender=previous.sender)
                    message.association_id = association_id
                    record.messages.append(message)

                if state.complete:
                    message = _audit_message(state, view, sender=state.sender)
                    message.association_id = association_id
                    record.messages.append(message)
                    state = _AssembledMessage(DIMSEMessage(), rules, sender=direction)

                states[key] = state

        for direction, state in states.items():
            if state.seen:
                message = _audit_message(
                    state,
                    view,
                    sender=direction,
                    truncated=truncated[0],
                )
                message.association_id = association_id
                record.messages.append(message)

        recording.append(record)

    return recording


# Compatibility aliases for common terminology
AssociationRecording = Recording
PresentationContextView = ContextView
RecordedAssociation = AssociationRecord
AcceptedContext = SOPClassContext
AuditStatus = MessageStatus
