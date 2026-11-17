# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

"""
Helpers for collecting SLC timer, counter and string elements.

The collector keeps the identity of an element separate from the identity of
the fields read from it.  A field address is always rendered from the parsed
``(file_type, file_number, element_number, field)`` components, so it can be
fed straight back into :meth:`SLCDriver.read` or :meth:`SLCDriver.collect`.

Status bits and numeric words are decoded from their own locations inside an
element.  Elements are split from a multi-element response with a fixed stride,
so a response can never borrow a word from the following element.
"""

import json
from collections import OrderedDict
from typing import Any, List, NamedTuple, Optional, Tuple

from .cip import (
    INT,
    PCCC_DATA_SIZE,
    PCCC_ELEMENT_FIELDS,
    PCCC_ELEMENT_FIELD_KINDS,
    PCCC_ELEMENT_FIELD_NAMES,
    PCCC_ELEMENT_TYPE_NAMES,
    PCCC_STRING,
    UINT,
)

__all__ = [
    "ElementAddress",
    "FieldResult",
    "ElementRecord",
    "SLCCollection",
    "format_address",
    "parse_element_data",
    "split_elements",
]


NUMERIC = "numeric"
UNSIGNED = "unsigned"
STATUS = "status"
DATA = "data"

ELEMENT_FILE_TYPES = frozenset(PCCC_ELEMENT_FIELDS)


def format_address(
    file_type: str,
    file_number: int,
    element_number: int,
    field: Optional[str] = None,
) -> str:
    """
    Render a canonical address from its parts.

    This is the only place an address string is produced for elements and
    fields.  It never uses a display name from a response.
    """
    address = f"{str(file_type).upper()}{int(file_number)}:{int(element_number)}"
    if field:
        address += f".{str(field).upper()}"
    return address


def _field_layout(file_type: str):
    return {
        name: (offset, kind, bit)
        for name, offset, kind, bit in PCCC_ELEMENT_FIELDS[file_type]
    }


class ElementAddress:
    """
    Parsed identity of an element or of one of its fields.

    ``parts`` is authoritative; :attr:`address` is only its canonical
    rendering.  Feeding :attr:`address` back to the driver reproduces the
    same parts.
    """

    __slots__ = ("file_type", "file_number", "element_number", "field")

    def __init__(
        self,
        file_type: str,
        file_number: int,
        element_number: int,
        field: Optional[str] = None,
    ):
        file_type = str(file_type).upper()
        if file_type not in ELEMENT_FILE_TYPES:
            raise ValueError(f"Unsupported SLC element file type: {file_type!r}")
        if field is not None:
            field = str(field).upper()
            if field not in PCCC_ELEMENT_FIELD_NAMES[file_type]:
                raise ValueError(
                    f"Unsupported field {field!r} for file type {file_type!r}"
                )
        self.file_type = file_type
        self.file_number = int(file_number)
        self.element_number = int(element_number)
        self.field = field

    @property
    def parts(self) -> Tuple[str, int, int, Optional[str]]:
        return (
            self.file_type,
            self.file_number,
            self.element_number,
            self.field,
        )

    @property
    def identity(self) -> Tuple[str, int, int]:
        return self.file_type, self.file_number, self.element_number

    @property
    def element_address(self) -> str:
        return format_address(
            self.file_type, self.file_number, self.element_number
        )

    @property
    def address(self) -> str:
        return format_address(
            self.file_type, self.file_number, self.element_number, self.field
        )

    @property
    def is_subfield(self) -> bool:
        return self.field is not None

    @property
    def type_name(self) -> str:
        return PCCC_ELEMENT_TYPE_NAMES[self.file_type]

    @property
    def kind(self) -> Optional[str]:
        if self.field is None:
            return "element"
        return PCCC_ELEMENT_FIELD_KINDS[self.file_type][self.field]

    def with_field(self, field: Optional[str]) -> "ElementAddress":
        return ElementAddress(
            self.file_type, self.file_number, self.element_number, field
        )

    def __eq__(self, other):
        return isinstance(other, ElementAddress) and self.parts == other.parts

    def __hash__(self):
        return hash(self.parts)

    def __str__(self):
        return self.address

    def __repr__(self):
        return (
            f"ElementAddress(file_type={self.file_type!r}, "
            f"file_number={self.file_number!r}, "
            f"element_number={self.element_number!r}, field={self.field!r})"
        )


class FieldResult(NamedTuple):
    """Result of one subfield, tied to the element that owns it."""

    field: str  #: field name, e.g. ``'PRE'``
    address: str  #: canonical address, e.g. ``'T4:0.PRE'``
    value: Any  #: value read, ``None`` if the field failed
    error: Optional[str] = None  #: error message if the field failed
    kind: Optional[str] = None  #: ``numeric``, ``status`` or ``data``
    element_address: Optional[str] = None  #: owning element address

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "address": self.address,
            "element": self.element_address,
            "kind": self.kind,
            "value": self.value,
            "error": self.error,
        }


class ElementRecord:
    """
    One SLC element and the fields declared for it.

    All fields share the element's :attr:`identity` and :attr:`address`.
    A record is ``complete`` when every field of the element was declared and
    read successfully, ``partial`` when at least one field was read but the
    element is not complete, and ``failed`` when no field could be read.
    """

    __slots__ = ("element", "declared", "fields", "error", "all_fields")

    def __init__(
        self,
        element: ElementAddress,
        declared: List[str],
        fields: "OrderedDict[str, FieldResult]",
        error: Optional[str] = None,
    ):
        self.element = element
        self.declared = tuple(declared)
        self.fields = fields
        self.error = error
        self.all_fields = tuple(PCCC_ELEMENT_FIELD_NAMES[element.file_type])

    @property
    def identity(self) -> Tuple[str, int, int]:
        return self.element.identity

    @property
    def address(self) -> str:
        return self.element.element_address

    @property
    def file_type(self) -> str:
        return self.element.file_type

    @property
    def file_number(self) -> int:
        return self.element.file_number

    @property
    def element_number(self) -> int:
        return self.element.element_number

    @property
    def type_name(self) -> str:
        return self.element.type_name

    @property
    def value(self) -> "OrderedDict[str, Any]":
        return OrderedDict(
            (name, result.value)
            for name, result in self.fields.items()
            if result.ok
        )

    @property
    def values(self) -> "OrderedDict[str, Any]":
        return self.value

    @property
    def errors(self) -> "OrderedDict[str, str]":
        return OrderedDict(
            (name, result.error)
            for name, result in self.fields.items()
            if not result.ok
        )

    @property
    def missing(self) -> List[str]:
        return [name for name, result in self.fields.items() if not result.ok]

    @property
    def ok(self) -> bool:
        """All declared fields were read successfully."""
        return not self.errors

    @property
    def complete(self) -> bool:
        """Every field of the element was declared and read successfully."""
        return self.ok and set(self.declared) == set(self.all_fields)

    @property
    def partial(self) -> bool:
        """Some coverage exists, but the element is not complete."""
        return bool(self.value) and not self.complete

    @property
    def failed(self) -> bool:
        """No field of the element could be read."""
        return not self.value

    def declare(self, *fields: str) -> None:
        """Add fields to the record's declared set, preserving order."""
        declared = list(self.declared)
        for field in fields:
            field = field.upper()
            if field not in declared:
                declared.append(field)
        self.declared = tuple(declared)

    def set_field(self, result: FieldResult) -> None:
        """Store the result of one field.

        A successful value already recorded for the field is never replaced
        by a later failure, so re-reading a subset cannot erase good data.
        """
        previous = self.fields.get(result.field)
        if previous is not None and previous.ok and not result.ok:
            return
        self.fields[result.field] = result

    def __getitem__(self, field: str):
        return self.fields[field.upper()].value

    def get(self, field: str, default=None):
        result = self.fields.get(field.upper())
        return result.value if result is not None and result.ok else default

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "file_type": self.file_type,
            "file_number": self.file_number,
            "element_number": self.element_number,
            "identity": list(self.identity),
            "type": self.type_name,
            "declared": list(self.declared),
            "complete": self.complete,
            "partial": self.partial,
            "failed": self.failed,
            "ok": self.ok,
            "value": dict(self.value),
            "fields": OrderedDict(
                (name, result.as_dict()) for name, result in self.fields.items()
            ),
            "errors": dict(self.errors),
            "missing": list(self.missing),
            "error": self.error,
        }

    def __repr__(self):
        status = "complete" if self.complete else (
            "partial" if self.partial else "failed"
        )
        return (
            f"ElementRecord(address={self.address!r}, type={self.type_name!r}, "
            f"status={status!r}, value={self.value!r})"
        )


class SLCCollection:
    """
    Result of :meth:`SLCDriver.collect`.

    Iterates over :class:`ElementRecord` objects in declaration order.  The
    ``complete``, ``partial`` and ``failed`` properties keep the three states
    separate instead of flattening them into one list.
    """

    __slots__ = ("records",)

    def __init__(self, records: List[ElementRecord]):
        self.records = list(records)

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.record(item)
        return self.records[item]

    def record(self, address: str) -> ElementRecord:
        """Return the record for an element address, e.g. ``'T4:0'``."""
        wanted = "".join(str(address).split()).upper()
        for record in self.records:
            if wanted == record.address.upper():
                return record
        raise KeyError(address)

    def get(self, address: str, default=None):
        try:
            return self.record(address)
        except KeyError:
            return default

    @property
    def complete(self) -> List[ElementRecord]:
        return [record for record in self.records if record.complete]

    @property
    def partial(self) -> List[ElementRecord]:
        return [record for record in self.records if record.partial]

    @property
    def failed(self) -> List[ElementRecord]:
        return [record for record in self.records if record.failed]

    def as_dict(self) -> dict:
        return {
            "records": [record.as_dict() for record in self.records],
            "complete": [record.address for record in self.complete],
            "partial": [record.address for record in self.partial],
            "failed": [record.address for record in self.failed],
        }

    export = as_dict

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.as_dict(), indent=indent)

    def __repr__(self):
        return (
            f"SLCCollection(records={len(self.records)!r}, "
            f"complete={len(self.complete)!r}, "
            f"partial={len(self.partial)!r}, failed={len(self.failed)!r})"
        )


def split_elements(file_type: str, data: bytes, count: int) -> List[bytes]:
    """
    Split a multi-element response into fixed-size element chunks.

    Each element is cut at exactly ``PCCC_DATA_SIZE`` bytes, so words from the
    next element cannot leak into the previous one.  Short responses yield
    short chunks; the field parser reports the missing fields individually.
    """
    stride = PCCC_DATA_SIZE[file_type]
    return [data[i * stride : (i + 1) * stride] for i in range(count)]


def parse_element_data(file_type: str, data: bytes) -> "OrderedDict[str, Tuple[Any, Optional[str]]]":
    """
    Decode every field of a single element from ``data``.

    Each field is decoded independently.  A failure only marks that field;
    the remaining fields keep their values.
    """
    file_type = file_type.upper()
    layout = _field_layout(file_type)
    results = OrderedDict()

    for name, (offset, kind, bit) in layout.items():
        try:
            if kind == STATUS:
                if len(data) < offset + 2:
                    raise ValueError(
                        f"missing status word for {name} (need {offset + 2} bytes, got {len(data)})"
                    )
                status_word = UINT.decode(data[offset : offset + 2])
                results[name] = (get_bit(status_word, bit), None)
            elif kind == NUMERIC:
                if len(data) < offset + 2:
                    raise ValueError(
                        f"missing word for {name} (need {offset + 2} bytes, got {len(data)})"
                    )
                results[name] = (INT.decode(data[offset : offset + 2]), None)
            elif kind == UNSIGNED:
                if len(data) < offset + 2:
                    raise ValueError(
                        f"missing word for {name} (need {offset + 2} bytes, got {len(data)})"
                    )
                results[name] = (UINT.decode(data[offset : offset + 2]), None)
            elif kind == DATA:
                stride = PCCC_DATA_SIZE[file_type]
                if len(data) < stride:
                    raise ValueError(
                        f"incomplete string element for {name} (need {stride} bytes, got {len(data)})"
                    )
                results[name] = (PCCC_STRING.decode(data[:stride]), None)
            else:  # pragma: no cover - layout is internal
                raise ValueError(f"unknown field kind {kind!r}")
        except Exception as err:
            results[name] = (None, f"{type(err).__name__}: {err}")

    return results


def get_bit(value: int, idx: int) -> bool:
    """:returns value of bit at position idx"""
    return (value & (1 << idx)) != 0
