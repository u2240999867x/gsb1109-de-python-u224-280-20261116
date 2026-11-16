# Copyright (C) 2026 Christian Ledermann
#
# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
"""
Named column contracts for KML extended data.

Different KML documents may carry the same field name (for example
``temperature``) through different mechanisms: an untyped ``Data`` name/value
pair or a ``SimpleData`` value whose type is declared by a ``Schema`` and
reached through ``schemaUrl``. Treating those fields as one column purely
because they share a name loses the typing information and, worse, lets a
schema from one document silently explain a value in another document.

The read models in this module give every observed column an explicit,
namespaced :class:`ColumnContract`. The scope of a contract is a single KML
document: ``schemaUrl`` references resolve only against ``Schema`` elements
declared in the same document. When the referenced schema (or the field within
it) cannot be resolved, the contract keeps the ``schema_url`` but reports
``declared_type=None`` instead of borrowing a declaration from elsewhere.

Values are converted according to the declared type independently. A value
that cannot be converted is isolated in its own :class:`ColumnValue` with the
original text preserved and an error recorded; sibling values and columns are
unaffected.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastkml import config
from fastkml.data import Data
from fastkml.data import ExtendedData
from fastkml.data import Schema
from fastkml.data import SchemaData
from fastkml.data import SimpleField
from fastkml.enums import DataType
from fastkml.gx.data import SimpleArrayField
from fastkml.utils import find_all

if TYPE_CHECKING:
    from fastkml.containers import Document

__all__ = [
    "ColumnContract",
    "ColumnValue",
    "column_contracts",
    "column_values",
]

logger = logging.getLogger(__name__)

_INTEGER_TYPES = frozenset(
    {
        DataType.int_,
        DataType.short,
        DataType.uint,
        DataType.ushort,
    },
)
_UNSIGNED_TYPES = frozenset(
    {
        DataType.uint,
        DataType.ushort,
    },
)
_FLOAT_TYPES = frozenset(
    {
        DataType.float_,
        DataType.double,
    },
)
_BOOL_TRUE = frozenset({"true", "1"})
_BOOL_FALSE = frozenset({"false", "0"})


@dataclass(frozen=True)
class ColumnContract:
    """
    The namespaced contract of an extended-data column.

    Attributes
    ----------
    name : str
        The field name as written in the document (``Data@name`` or
        ``SimpleData@name``).
    namespace : str
        The XML namespace URI the value elements live in. Untyped ``Data`` and
        schema-backed ``SimpleData`` use the KML namespace, ``gx`` array values
        use the Google extension namespace.
    schema_url : Optional[str]
        The ``schemaUrl`` of the enclosing ``SchemaData``, or ``None`` for an
        untyped ``Data`` element. The reference is kept verbatim even when it
        cannot be resolved inside the document.
    declared_type : Optional[DataType]
        The type declared by the matching ``SimpleField`` in the document's own
        schema. ``None`` for untyped ``Data`` and whenever the referenced schema
        or field cannot be resolved in this document.
    array : bool
        ``True`` when the column is backed by a ``gx:SimpleArrayData``,
        ``False`` for ordinary scalar values.

    """

    name: str
    namespace: str
    schema_url: str | None
    declared_type: DataType | None
    array: bool


@dataclass(frozen=True)
class ColumnValue:
    """
    A single observed value together with its :class:`ColumnContract`.

    Attributes
    ----------
    contract : ColumnContract
        The namespaced contract the value was observed against.
    raw : Optional[str]
        The original value text exactly as it appeared in the KML document. It
        is always retained, including when the conversion failed.
    value : object
        The value converted according to the contract's declared type. For an
        untyped contract (``declared_type is None``) the raw text is returned
        unchanged. ``None`` when the conversion failed or when no value was
        present.
    error : Optional[str]
        A human-readable reason when the conversion failed, otherwise ``None``.
        A failed conversion isolates only this value; other values, including
        the remaining elements of an array column, are converted on their own.

    """

    contract: ColumnContract
    raw: str | None
    value: object
    error: str | None


def _namespace_uri(ns: str | None, default: str) -> str:
    namespace = ns or default
    if namespace.startswith("{") and namespace.endswith("}"):
        return namespace[1:-1]
    return namespace


def _index_schemata(schemata: Iterable[Schema]) -> dict[str, Schema]:
    return {schema.id: schema for schema in schemata if schema.id}


def _resolve_schema(
    schema_url: str | None,
    schema_index: dict[str, Schema],
) -> Schema | None:
    if not schema_url:
        return None
    fragment = urlparse(schema_url).fragment
    if not fragment:
        return None
    return schema_index.get(fragment)


def _find_field(
    schema: Schema,
    name: str | None,
    *,
    array: bool,
) -> SimpleField | SimpleArrayField | None:
    fields = schema.array_fields if array else schema.fields
    return next((field for field in fields if field.name == name), None)


def _coerce_bool(text: str, raw: str) -> bool:
    lowered = text.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    msg = f"invalid boolean value {raw!r}"
    raise ValueError(msg)


def _coerce(raw: str | None, data_type: DataType) -> object:
    if raw is None:
        return None
    if data_type is DataType.string:
        return raw
    text = raw.strip()
    if data_type in _INTEGER_TYPES:
        number = int(text)
        if data_type in _UNSIGNED_TYPES and number < 0:
            msg = f"unsigned type {data_type.value} cannot hold {raw!r}"
            raise ValueError(msg)
        return number
    if data_type in _FLOAT_TYPES:
        return float(text)
    if data_type is DataType.bool_:
        return _coerce_bool(text, raw)
    msg = f"unsupported data type {data_type!r}"
    raise ValueError(msg)


def _coerce_value(contract: ColumnContract, raw: str | None) -> ColumnValue:
    if contract.declared_type is None:
        return ColumnValue(contract=contract, raw=raw, value=raw, error=None)
    try:
        converted = _coerce(raw, contract.declared_type)
    except (ValueError, OverflowError) as error:
        logger.warning(
            "Could not convert %r to %s for column %r: %s",
            raw,
            contract.declared_type.value,
            contract.name,
            error,
        )
        return ColumnValue(
            contract=contract,
            raw=raw,
            value=None,
            error=str(error),
        )
    return ColumnValue(contract=contract, raw=raw, value=converted, error=None)


def _untyped_value(data: Data) -> ColumnValue:
    contract = ColumnContract(
        name=data.name or "",
        namespace=_namespace_uri(data.ns, config.KMLNS),
        schema_url=None,
        declared_type=None,
        array=False,
    )
    return _coerce_value(contract, data.value)


def _schema_data_values(
    schema_data: SchemaData,
    schema_index: dict[str, Schema],
) -> list[ColumnValue]:
    schema = _resolve_schema(schema_data.schema_url, schema_index)
    values: list[ColumnValue] = []
    scalar_namespace = _namespace_uri(schema_data.ns, config.KMLNS)
    for simple_data in schema_data.data:
        field = (
            _find_field(schema, simple_data.name, array=False)
            if schema is not None
            else None
        )
        contract = ColumnContract(
            name=simple_data.name or "",
            namespace=scalar_namespace,
            schema_url=schema_data.schema_url,
            declared_type=field.type_ if field is not None else None,
            array=False,
        )
        values.append(_coerce_value(contract, simple_data.value))
    for array_data in schema_data.array_data:
        field = (
            _find_field(schema, array_data.name, array=True)
            if schema is not None
            else None
        )
        contract = ColumnContract(
            name=array_data.name or "",
            namespace=_namespace_uri(array_data.ns, config.GXNS),
            schema_url=schema_data.schema_url,
            declared_type=field.type_ if field is not None else None,
            array=True,
        )
        values.extend(_coerce_value(contract, item) for item in array_data.data)
    return values


def column_values(document: "Document") -> list[ColumnValue]:
    """
    Return every extended-data value observed in a document.

    The ``schemaUrl`` references of the document are resolved exclusively
    against the ``Schema`` elements declared in that same document. A value
    whose conversion fails is returned on its own with the raw text and an
    error message instead of interrupting the walk.

    Args:
    ----
        document: The KML ``Document`` to read the extended data from.

    Returns:
    -------
        list[ColumnValue]: The observed values in traversal order. Within a
            single ``ExtendedData`` element, untyped ``Data`` values precede
            ``SchemaData`` values because the registry groups them by class.

    """
    schema_index = _index_schemata(document.schemata)
    values: list[ColumnValue] = []
    for extended_data in find_all(document, of_type=ExtendedData):
        for element in extended_data.elements:
            if isinstance(element, Data):
                values.append(_untyped_value(element))
            else:
                values.extend(_schema_data_values(element, schema_index))
    return values


def column_contracts(document: "Document") -> list[ColumnContract]:
    """
    Return the distinct column contracts observed in a document.

    Contracts are deduplicated by their full namespaced identity and returned
    in the order the corresponding column is first encountered. Two columns
    with the same name but different ``schema_url`` or ``declared_type`` values
    remain separate contracts.

    Args:
    ----
        document: The KML ``Document`` to read the contracts from.

    Returns:
    -------
        list[ColumnContract]: The distinct observed column contracts.

    """
    contracts: list[ColumnContract] = []
    seen: set[tuple[object, ...]] = set()
    for column_value in column_values(document):
        contract = column_value.contract
        key = (
            contract.name,
            contract.namespace,
            contract.schema_url,
            contract.declared_type,
            contract.array,
        )
        if key not in seen:
            seen.add(key)
            contracts.append(contract)
    return contracts
