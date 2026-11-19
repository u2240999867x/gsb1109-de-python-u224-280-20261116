"""Reassemble DIMSE messages from an offline P-DATA-TF recording.

The recording is treated as a flat byte stream of P-DATA-TF PDUs. PDVs are
grouped per presentation context ID, so fragments belonging to different
contexts may be interleaved arbitrarily. Byte ranges are always reported
against the original stream offsets.

A missing or truncated fragment is never recovered by scanning the remaining
payload for familiar DICOM tags. The affected context is marked incomplete and
left alone; messages from other contexts that did complete are still reported.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterator

from pynetdicom.dimse_messages import _MESSAGE_TYPES
from pydicom.filereader import read_dataset

PDATA = 0x04

# PS3.7 Table 9-3 and 9-4: elements that are always present for a type.
_REQUIRED_COMMAND_ELEMENTS = {
    "C-STORE-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID", "Priority"),
    "C-STORE-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "C-FIND-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID", "Priority"),
    "C-FIND-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "C-GET-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID", "Priority"),
    "C-GET-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "C-MOVE-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID", "Priority"),
    "C-MOVE-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "C-ECHO-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID"),
    "C-ECHO-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-EVENT-REPORT-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID"),
    "N-EVENT-REPORT-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-GET-RQ": ("RequestedSOPClassUID", "CommandField", "MessageID"),
    "N-GET-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-SET-RQ": ("RequestedSOPClassUID", "CommandField", "MessageID"),
    "N-SET-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-ACTION-RQ": ("RequestedSOPClassUID", "CommandField", "MessageID"),
    "N-ACTION-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-CREATE-RQ": ("AffectedSOPClassUID", "CommandField", "MessageID"),
    "N-CREATE-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "N-DELETE-RQ": ("RequestedSOPClassUID", "CommandField", "MessageID"),
    "N-DELETE-RSP": ("CommandField", "MessageIDBeingRespondedTo", "Status"),
    "C-CANCEL-RQ": ("CommandField", "MessageIDBeingRespondedTo"),
}


def _read_command_set(encoded: bytes):
    """Return ``(dataset, bytes consumed)`` for an implicit LE command set."""
    stream = BytesIO(encoded)
    dataset = read_dataset(stream, is_implicit_VR=True, is_little_endian=True)
    return dataset, stream.tell()


def _non_command_elements(dataset) -> list[str]:
    """Return tags outside command group 0000 (a sign of lost fragments)."""
    return [
        str(element.tag)
        for element in dataset
        if element.tag.group != 0x0000
    ]


class RecordingError(Exception):
    """Raised when the byte stream is not a well formed P-DATA-TF sequence."""


@dataclass(frozen=True)
class PDV:
    """A single Presentation Data Value Item and its original byte ranges."""

    index: int
    context_id: int
    pdu_range: tuple[int, int]
    item_range: tuple[int, int]
    value_range: tuple[int, int]
    data_range: tuple[int, int]
    mchb: int

    @property
    def is_command(self) -> bool:
        return bool(self.mchb & 0x01)

    @property
    def is_last(self) -> bool:
        return bool(self.mchb & 0x02)

    @property
    def kind(self) -> str:
        return "CMD" if self.is_command else "DAT"


@dataclass
class Message:
    """A DIMSE message assembled for one presentation context."""

    context_id: int
    command_pdvs: list[PDV] = field(default_factory=list)
    dataset_pdvs: list[PDV] = field(default_factory=list)
    command_set: object | None = None
    command_length_ok: bool | None = None
    command_error: str | None = None
    dataset_error: str | None = None

    @property
    def command_complete(self) -> bool:
        return bool(self.command_pdvs) and self.command_pdvs[-1].is_last

    @property
    def dataset_started(self) -> bool:
        return bool(self.dataset_pdvs)

    @property
    def dataset_complete(self) -> bool:
        return bool(self.dataset_pdvs) and self.dataset_pdvs[-1].is_last

    @property
    def dataset_expected(self) -> bool:
        if self.command_set is None:
            return False

        return getattr(self.command_set, "CommandDataSetType", 0x0101) != 0x0101

    @property
    def complete(self) -> bool:
        if not self.command_complete or self.command_error:
            return False

        if self.dataset_expected:
            return self.dataset_complete and not self.dataset_error

        return not self.dataset_started

    @property
    def overall_range(self) -> tuple[int, int] | None:
        ranges = [
            p.data_range for p in self.command_pdvs + self.dataset_pdvs
        ]
        if not ranges:
            return None

        return min(r[0] for r in ranges), max(r[1] for r in ranges)


def iter_pdvs(stream: bytes) -> Iterator[PDV]:
    """Yield every PDV in `stream`, validating PDU and item lengths."""
    offset = 0
    index = 0
    while offset < len(stream):
        if len(stream) - offset < 6:
            raise RecordingError(
                f"truncated PDU header at offset {offset}: "
                f"{len(stream) - offset} byte(s) available, 6 required"
            )

        pdu_type = stream[offset]
        pdu_length = int.from_bytes(stream[offset + 2 : offset + 6], "big")
        pdu_end = offset + 6 + pdu_length
        if pdu_type != PDATA:
            raise RecordingError(
                f"unexpected PDU type 0x{pdu_type:02x} at offset {offset}, "
                f"expected P-DATA-TF (0x04)"
            )
        if pdu_end > len(stream):
            raise RecordingError(
                f"truncated PDU at offset {offset}: declared end {pdu_end}, "
                f"stream ends at {len(stream)}"
            )

        item = offset + 6
        while item < pdu_end:
            if pdu_end - item < 5:
                raise RecordingError(
                    f"truncated PDV item at offset {item}: "
                    f"{pdu_end - item} byte(s) available, 5 required"
                )

            item_length = int.from_bytes(stream[item : item + 4], "big")
            item_end = item + 4 + item_length
            if item_length < 1 or item_end > pdu_end:
                raise RecordingError(
                    f"invalid PDV item length {item_length} at offset {item}: "
                    f"item would end at {item_end}, PDU ends at {pdu_end}"
                )

            value_start = item + 5
            mchb = stream[value_start]
            yield PDV(
                index=index,
                context_id=stream[item + 4],
                pdu_range=(offset, pdu_end),
                item_range=(item, item_end),
                value_range=(value_start, item_end),
                data_range=(value_start + 1, item_end),
                mchb=mchb,
            )
            index += 1
            item = item_end

        offset = pdu_end


class Reassembler:
    """Group PDVs by presentation context and build DIMSE messages."""

    def __init__(self, stream: bytes) -> None:
        self._stream = stream
        self.messages: dict[int, Message] = {}
        self.finished: list[Message] = []
        self.errors: list[str] = []

    def _current(self, context_id: int) -> Message:
        if context_id not in self.messages:
            self.messages[context_id] = Message(context_id=context_id)

        return self.messages[context_id]

    def _finish(self, context_id: int) -> None:
        self.finished.append(self.messages.pop(context_id))

    def feed(self, pdv: PDV) -> None:
        message = self._current(pdv.context_id)
        if message.complete:
            self._finish(pdv.context_id)
            message = self._current(pdv.context_id)

        if message.dataset_started and pdv.is_command:
            message.dataset_error = (
                f"command PDV {pdv.index} at {pdv.data_range} arrived after the "
                f"data set had started at {message.dataset_pdvs[0].data_range}"
            )
            self.errors.append(
                f"context {pdv.context_id}: {message.dataset_error}"
            )
            return

        if pdv.is_command:
            if message.command_complete:
                message.command_error = (
                    f"command PDV {pdv.index} at {pdv.data_range} arrived after "
                    f"the command set ended at {message.command_pdvs[-1].data_range}"
                )
                self.errors.append(
                    f"context {pdv.context_id}: {message.command_error}"
                )
                return

            message.command_pdvs.append(pdv)
            if pdv.is_last:
                self._decode_command(message)
            return

        if not message.command_complete:
            message.dataset_error = (
                f"data set PDV {pdv.index} at {pdv.data_range} arrived before "
                f"the command set was complete"
            )
            self.errors.append(f"context {pdv.context_id}: {message.dataset_error}")
            return

        message.dataset_pdvs.append(pdv)
        if pdv.is_last:
            self._finish(pdv.context_id)

    def _decode_command(self, message: Message) -> None:
        chunks = []
        for pdv in message.command_pdvs:
            start, end = pdv.data_range
            chunks.append(self._stream[start:end])
        encoded = b"".join(chunks)

        try:
            message.command_set, consumed = _read_command_set(encoded)
        except Exception as exc:
            message.command_error = (
                f"command set at {message.command_pdvs[0].data_range}-"
                f"{message.command_pdvs[-1].data_range} could not be decoded: {exc}"
            )
            self.errors.append(f"context {message.context_id}: {message.command_error}")
            return

        # A complete command set parses every element and ends exactly at the
        # end of the assembled fragments. If it does not, a fragment is
        # missing; do not resynchronise by hunting for known tags.
        message.command_length_ok = consumed == len(encoded)
        if not message.command_length_ok:
            message.command_error = (
                f"command set assembled from "
                f"{message.command_pdvs[0].data_range[0]}-"
                f"{message.command_pdvs[-1].data_range[1]} is {len(encoded)} "
                f"byte(s) but its command elements end at byte {consumed}; "
                f"a fragment is missing or out of order"
            )
            self.errors.append(f"context {message.context_id}: {message.command_error}")
            return

        command_field = getattr(message.command_set, "CommandField", None)
        type_name = _MESSAGE_TYPES.get(command_field, (None,))[0]
        missing = [
            keyword
            for keyword in _REQUIRED_COMMAND_ELEMENTS.get(type_name, ())
            if keyword not in message.command_set
        ]
        if missing:
            message.command_error = (
                f"command set at {message.command_pdvs[0].data_range}-"
                f"{message.command_pdvs[-1].data_range} lacks required "
                f"element(s) {', '.join(missing)}; a fragment is missing"
            )
            self.errors.append(f"context {message.context_id}: {message.command_error}")
            return

        stray = _non_command_elements(message.command_set)
        if stray:
            message.command_error = (
                f"command set at {message.command_pdvs[0].data_range}-"
                f"{message.command_pdvs[-1].data_range} contains non-command "
                f"element(s) {', '.join(stray)}; a fragment is missing or out of "
                f"order and the stream was not resynchronised"
            )
            self.errors.append(f"context {message.context_id}: {message.command_error}")
            return

        if not message.dataset_expected:
            self._finish(message.context_id)

    def drain(self) -> list[Message]:
        """Return completed messages; leave only incomplete contexts pending."""
        finished = list(self.finished)
        self.finished.clear()
        return finished

    def pending(self) -> list[Message]:
        return list(self.messages.values())


def analyse(stream: bytes) -> Reassembler:
    """Reassemble `stream`, returning the reassembler state."""
    reassembler = Reassembler(stream)
    try:
        for pdv in iter_pdvs(stream):
            reassembler.feed(pdv)
    except RecordingError as exc:
        reassembler.errors.append(str(exc))

    return reassembler


def _fmt_range(value: tuple[int, int] | None) -> str:
    return "n/a" if value is None else f"[{value[0]}, {value[1]})"


def _describe(message: Message) -> dict:
    command_set = message.command_set
    command_field = getattr(command_set, "CommandField", None)
    type_name = (
        _MESSAGE_TYPES[command_field][0]
        if command_field in _MESSAGE_TYPES
        else None
    )
    data = {
        "context_id": message.context_id,
        "message_type": type_name,
        "command_field": (
            None if command_field is None else f"0x{command_field:04x}"
        ),
        "message_id": getattr(command_set, "MessageID", None),
        "message_id_being_responded_to": getattr(
            command_set, "MessageIDBeingRespondedTo", None
        ),
        "command_data_set_type": (
            None
            if command_set is None
            else f"0x{getattr(command_set, 'CommandDataSetType', 0):04x}"
        ),
        "overall_range": _fmt_range(message.overall_range),
        "command_pdvs": [
            {
                "index": pdv.index,
                "pdu": _fmt_range(pdv.pdu_range),
                "data": _fmt_range(pdv.data_range),
                "mchb": f"0x{pdv.mchb:02x}",
                "last": pdv.is_last,
            }
            for pdv in message.command_pdvs
        ],
        "command_end": (
            "confirmed"
            if message.command_complete
            else "not reached"
        ),
        "command_bytes": sum(
            pdv.data_range[1] - pdv.data_range[0] for pdv in message.command_pdvs
        ),
        "command_group_length_ok": message.command_length_ok,
        "dataset_pdvs": [
            {
                "index": pdv.index,
                "pdu": _fmt_range(pdv.pdu_range),
                "data": _fmt_range(pdv.data_range),
                "mchb": f"0x{pdv.mchb:02x}",
                "last": pdv.is_last,
            }
            for pdv in message.dataset_pdvs
        ],
        "dataset_expected": message.dataset_expected,
        "dataset_end": (
            "not applicable"
            if not message.dataset_expected
            else "confirmed" if message.dataset_complete else "not reached"
        ),
        "dataset_bytes": sum(
            pdv.data_range[1] - pdv.data_range[0] for pdv in message.dataset_pdvs
        ),
        "complete": message.complete,
        "errors": [
            error
            for error in (message.command_error, message.dataset_error)
            if error
        ],
    }
    return data


def report(stream: bytes) -> dict:
    reassembler = analyse(stream)
    return {
        "stream_bytes": len(stream),
        "messages": [_describe(m) for m in reassembler.drain()],
        "incomplete": [_describe(m) for m in reassembler.pending()],
        "errors": reassembler.errors,
    }


def _print_message(item: dict, pending: bool = False) -> None:
    label = "incomplete" if pending else "message"
    print(f"\n{label}: {item['message_type']} (context {item['context_id']})")
    if item["message_id"] is not None:
        print(f"  message id: {item['message_id']}")
    if item["message_id_being_responded_to"] is not None:
        print(
            "  responding to: "
            f"{item['message_id_being_responded_to']}"
        )
    print(f"  stream range: {item['overall_range']}")

    for line in item["command_pdvs"]:
        print(
            f"  command PDV {line['index']}: pdu {line['pdu']} "
            f"data {line['data']} MCHB {line['mchb']}"
        )
    print(f"  command end: {item['command_end']} ({item['command_bytes']} byte(s))")

    if item["dataset_expected"]:
        for line in item["dataset_pdvs"]:
            print(
                f"  data set PDV {line['index']}: pdu {line['pdu']} "
                f"data {line['data']} MCHB {line['mchb']}"
            )
        print(
            f"  data set end: {item['dataset_end']} "
            f"({item['dataset_bytes']} byte(s))"
        )
    else:
        print("  data set: none (CommandDataSetType 0x0101)")

    for error in item["errors"]:
        print(f"  problem: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument(
        "--json", action="store_true", help="write the report as JSON"
    )
    args = parser.parse_args(argv)

    try:
        stream = args.recording.read_bytes()
    except OSError as exc:
        print(f"error: cannot read {args.recording}: {exc}", file=sys.stderr)
        return 2

    result = report(stream)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"stream: {result['stream_bytes']} byte(s)")
        for item in result["messages"]:
            _print_message(item)
        if result["messages"] and result["incomplete"]:
            print("\n-- some contexts did not complete --")
        for item in result["incomplete"]:
            _print_message(item, pending=True)
        for error in result["errors"]:
            print(f"error: {error}", file=sys.stderr)

    return 0 if not result["errors"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
