#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Build supplementary SNMP collection plans from MIB metadata.

Given a handful of business fields missing from an inspection, the planner
works out which additional variables must be read so that those fields can be
interpreted and correlated.

Dependencies are derived from the MIB itself:

* ``INDEX`` columns identify the table row a field belongs to;
* the ``RowStatus`` column tells whether that row is usable;
* ``UNITS`` is local metadata and never becomes an SNMP request.

Every OID is requested at most once, even when several fields share the same
dependency. Previously confirmed variables are reused only when their MIB
revision and syntax are compatible. Rows are keyed by device generation as
well as by index values, so results from different generations are never
spliced into one another.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from pysnmp.smi import error
from pysnmp.smi.rfc1902 import ObjectIdentity

__all__ = [
    "CollectionField",
    "CollectionPlan",
    "CollectionPlanner",
    "CollectionRequest",
    "CollectionResult",
    "CollectionRow",
    "ConfirmedVariable",
    "FieldMetadata",
    "IndexRef",
    "build_collection_plan",
    "plan_supplementary",
]


Oid = Tuple[int, ...]
RowKey = Tuple[Optional[str], Tuple[Any, ...]]


def _as_oid(value) -> Oid:
    if isinstance(value, str):
        return tuple(int(part) for part in value.split(".") if part)
    return tuple(value)


def _as_revision(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        return str(value[0]) if value else None
    return str(value)


@dataclass(frozen=True)
class IndexRef:
    """A table index column and, when known, its instance OID."""

    module: str
    symbol: str
    oid: Oid
    implied: bool = False


@dataclass(frozen=True)
class CollectionField:
    """A business field that should be present after collection."""

    module: str
    symbol: str
    indices: Tuple[Any, ...] = ()
    generation: Optional[str] = None
    label: Optional[str] = None

    def __post_init__(self):
        """Normalize the index sequence."""
        object.__setattr__(self, "indices", tuple(self.indices))

    @property
    def key(self) -> str:
        """Return a stable human-readable field key."""
        return self.label or f"{self.module}::{self.symbol}"


@dataclass(frozen=True)
class ConfirmedVariable:
    """A variable whose value was already confirmed by an earlier read."""

    oid: Oid
    value: Any
    generation: Optional[str] = None
    mib_revision: Optional[str] = None
    syntax: Optional[str] = None

    def __post_init__(self):
        """Normalize the variable OID and revision."""
        object.__setattr__(self, "oid", _as_oid(self.oid))
        object.__setattr__(self, "mib_revision", _as_revision(self.mib_revision))


@dataclass
class CollectionRequest:
    """One SNMP variable that has to be requested."""

    oid: Oid
    module: str
    symbol: str
    reason: str
    generation: Optional[str] = None
    fields: Tuple[str, ...] = ()

    def prettyPrint(self):  # noqa: N802
        """Return a human-readable request description."""
        return "{}::{} {} [{}]".format(
            self.module,
            self.symbol,
            ".".join(str(x) for x in self.oid),
            self.reason,
        )


@dataclass
class FieldMetadata:
    """MIB metadata needed to interpret a business field."""

    field: CollectionField
    module: str
    symbol: str
    oid: Oid
    instance_oid: Oid
    syntax: str
    units: str
    status: str
    max_access: str
    index_refs: Tuple[IndexRef, ...] = ()
    row_status_ref: Optional[IndexRef] = None
    row_status_value: Any = None
    mib_revision: Optional[str] = None

    @property
    def key(self) -> str:
        """Return the field key."""
        return self.field.key

    @property
    def generation(self) -> Optional[str]:
        """Return the device generation this field belongs to."""
        return self.field.generation

    @property
    def indices(self) -> Tuple[Any, ...]:
        """Return the field instance index values."""
        return self.field.indices


@dataclass
class CollectionRow:
    """A row assembled for exactly one device generation."""

    generation: Optional[str]
    indices: Tuple[Any, ...]
    values: Dict[str, Any] = field(default_factory=dict)
    missing: Tuple[str, ...] = ()
    metadata: Dict[str, FieldMetadata] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Return `True` when every planned field has a value."""
        return not self.missing

    def get(self, key, default=None):
        """Return a field value by key."""
        return self.values.get(key, default)


@dataclass
class CollectionResult:
    """Rows assembled from confirmed and newly read variables."""

    rows: Tuple[CollectionRow, ...]

    def by_generation(self, generation) -> Tuple[CollectionRow, ...]:
        """Return only rows belonging to *generation*."""
        return tuple(row for row in self.rows if row.generation == generation)

    def get(self, generation, indices):
        """Return a row by generation and index values, if present."""
        indices = tuple(indices)
        for row in self.rows:
            if row.generation == generation and row.indices == indices:
                return row
        return None


class CollectionPlanner:
    """Derive a supplementary collection plan from MIB metadata."""

    def __init__(
        self,
        mibViewController,
        confirmed: Optional[Iterable[ConfirmedVariable] | Mapping] = None,
        mib_revision: Optional[Mapping[str, str]] = None,
        generation: Optional[str] = None,
    ):
        """Create a planner bound to a MIB view controller.

        *generation* is the default device generation. Confirmed variables
        without an explicit generation inherit it.
        """
        self.mibViewController = mibViewController
        self.generation = generation
        self.mib_revision = {
            module: _as_revision(revision)
            for module, revision in (mib_revision or {}).items()
        }
        self._revision_cache: Dict[str, Optional[str]] = {}
        self._row_status_cache: Dict[RowKey, Optional[IndexRef]] = {}
        self._confirmed: Dict[Oid, Tuple[ConfirmedVariable, ...]] = {}
        self._add_confirmed(confirmed or ())

    # Confirmed variables

    def _add_confirmed(self, confirmed):
        if isinstance(confirmed, Mapping):
            confirmed = [
                ConfirmedVariable(oid, value) for oid, value in confirmed.items()
            ]

        for variable in confirmed:
            if not isinstance(variable, ConfirmedVariable):
                variable = ConfirmedVariable(*variable)
            if variable.generation is None and self.generation is not None:
                variable = ConfirmedVariable(
                    variable.oid,
                    variable.value,
                    self.generation,
                    variable.mib_revision,
                    variable.syntax,
                )
            self._confirmed.setdefault(variable.oid, ())
            self._confirmed[variable.oid] += (variable,)

    def _module_revision(self, module: str) -> Optional[str]:
        if module in self.mib_revision:
            return self.mib_revision[module]
        if module in self._revision_cache:
            return self._revision_cache[module]

        builder = self.mibViewController.mibBuilder
        revision = None
        try:
            (ModuleIdentity,) = builder.import_symbols("SNMPv2-SMI", "ModuleIdentity")
        except error.SmiError:
            ModuleIdentity = ()  # type: ignore

        for symbol in builder.mibSymbols.get(module, {}).values():
            if isinstance(symbol, ModuleIdentity):
                revisions = symbol.getRevisions()
                if revisions:
                    revision = _as_revision(revisions[0])
                break

        self._revision_cache[module] = revision
        return revision

    @staticmethod
    def _syntax_name(node) -> str:
        try:
            return node.getSyntax().__class__.__name__
        except AttributeError:
            return ""

    def _compatible(
        self,
        variable: ConfirmedVariable,
        oid: Oid,
        generation: Optional[str],
        module: str,
        syntax: Optional[str],
    ) -> bool:
        if variable.oid != oid:
            return False

        if variable.generation != generation:
            return False

        expected_revision = self._module_revision(module)
        if (
            variable.mib_revision is not None
            and variable.mib_revision != expected_revision
        ):
            return False

        if syntax and variable.syntax and variable.syntax != syntax:
            return False

        return True

    def _confirmed_value(
        self,
        oid: Oid,
        generation: Optional[str],
        module: str,
        syntax: Optional[str] = None,
    ):
        for variable in self._confirmed.get(oid, ()):
            if self._compatible(variable, oid, generation, module, syntax):
                return variable
        return None

    # MIB metadata

    def describe(self, field) -> FieldMetadata:
        """Return the MIB metadata for one collection field."""
        field = self._coerce_field(field)
        if field.generation is None and self.generation is not None:
            field = CollectionField(
                field.module,
                field.symbol,
                field.indices,
                self.generation,
                field.label,
            )
        return self._metadata(field)

    @staticmethod
    def _coerce_field(field) -> CollectionField:
        if isinstance(field, CollectionField):
            return field
        if isinstance(field, ObjectIdentity):
            module, symbol, indices = field.get_mib_symbol()
            return CollectionField(module, symbol, indices)
        if isinstance(field, (tuple, list)):
            if len(field) >= 2 and isinstance(field[0], str):
                return CollectionField(field[0], field[1], tuple(field[2:]))
            return CollectionField("", "", tuple(field))
        raise error.SmiError(f"Cannot plan collection for {field!r}")

    def _column_module(self, column) -> str:
        module, _, _ = self.mibViewController.get_node_location(column.name)
        return module

    def _row_status_ref(self, row, indices: Tuple[Any, ...]) -> Optional[IndexRef]:
        cache_key = (row.name, tuple(indices))
        if cache_key in self._row_status_cache:
            cached = self._row_status_cache[cache_key]
            return cached

        builder = self.mibViewController.mibBuilder
        try:
            (RowStatus,) = builder.import_symbols("SNMPv2-TC", "RowStatus")
            (MibTableColumn,) = builder.import_symbols("SNMPv2-SMI", "MibTableColumn")
        except error.SmiError:
            self._row_status_cache[cache_key] = None
            return None

        ref = None
        for module_symbols in builder.mibSymbols.values():
            for column in module_symbols.values():
                if not isinstance(column, MibTableColumn):
                    continue
                if column.name[:-1] != row.name:
                    continue
                if not isinstance(column.getSyntax(), RowStatus):
                    continue
                oid = column.name
                if indices:
                    oid += row.getInstIdFromIndices(*indices)
                ref = IndexRef(
                    module=self._column_module(column),
                    symbol=column.getLabel(),
                    oid=oid,
                )
                break
            if ref is not None:
                break

        self._row_status_cache[cache_key] = ref
        return ref

    def _metadata(self, field: CollectionField) -> FieldMetadata:
        identity = ObjectIdentity(field.module, field.symbol, *field.indices)
        identity.resolve_with_mib(self.mibViewController)
        node = identity.get_mib_node()
        instance_oid = tuple(identity.get_oid())

        index_refs: Tuple[IndexRef, ...] = ()
        row_status_ref = None

        (MibScalar, MibTableColumn) = self.mibViewController.mibBuilder.import_symbols(
            "SNMPv2-SMI", "MibScalar", "MibTableColumn"
        )
        if isinstance(node, MibTableColumn):
            row_module, row_symbol, _ = self.mibViewController.get_node_location(
                node.name[:-1]
            )
            (row,) = self.mibViewController.mibBuilder.import_symbols(
                row_module, row_symbol
            )

            refs = []
            for implied, module, symbol in row.getIndexNames():
                (column,) = self.mibViewController.mibBuilder.import_symbols(
                    module, symbol
                )
                oid = column.name
                if field.indices:
                    oid += row.getInstIdFromIndices(*field.indices)
                refs.append(IndexRef(module, symbol, oid, bool(implied)))
            index_refs = tuple(refs)
            row_status_ref = self._row_status_ref(row, field.indices)
        elif isinstance(node, MibScalar) and not field.indices:
            instance_oid = tuple(node.name) + (0,)

        row_status_value = None
        if row_status_ref is not None:
            confirmed = self._confirmed_value(
                row_status_ref.oid,
                field.generation,
                row_status_ref.module,
                "RowStatus",
            )
            if confirmed is not None:
                row_status_value = confirmed.value

        module, symbol, _ = self.mibViewController.get_node_location(node.name)
        return FieldMetadata(
            field=field,
            module=module,
            symbol=symbol,
            oid=tuple(node.name),
            instance_oid=instance_oid,
            syntax=self._syntax_name(node),
            units=node.getUnits() if hasattr(node, "getUnits") else "",
            status=node.getStatus() if hasattr(node, "getStatus") else "",
            max_access=node.getMaxAccess() if hasattr(node, "getMaxAccess") else "",
            index_refs=index_refs,
            row_status_ref=row_status_ref,
            row_status_value=row_status_value,
            mib_revision=self._module_revision(module),
        )

    # Planning

    def _reuse_or_request(
        self,
        requests: Dict[Tuple[Optional[str], Oid], CollectionRequest],
        reused: Dict[Tuple[Optional[str], Oid], ConfirmedVariable],
        oid: Oid,
        field: CollectionField,
        module: str,
        symbol: str,
        reason: str,
        syntax: Optional[str],
    ):
        key = (field.generation, oid)

        confirmed = self._confirmed_value(oid, field.generation, module, syntax)
        if confirmed is not None:
            reused.setdefault(key, confirmed)
            return

        request = requests.get(key)
        if request is None:
            requests[key] = CollectionRequest(
                oid=oid,
                module=module,
                symbol=symbol,
                reason=reason,
                generation=field.generation,
                fields=(field.key,),
            )
            return

        reasons = set(request.reason.split("+"))
        reasons.add(reason)
        request.reason = "+".join(sorted(reasons))
        if field.key not in request.fields:
            request.fields += (field.key,)

    def plan(
        self, fields: Iterable, generation: Optional[str] = None
    ) -> "CollectionPlan":
        """Build a supplementary collection plan for *fields*.

        Fields without an explicit generation inherit *generation*. Requests
        are de-duplicated by generation and OID.
        """
        requests: Dict[Tuple[Optional[str], Oid], CollectionRequest] = {}
        reused: Dict[Tuple[Optional[str], Oid], ConfirmedVariable] = {}
        metadata = []

        for raw_field in fields:
            field = self._coerce_field(raw_field)
            if field.generation is None:
                inherited = generation if generation is not None else self.generation
                if inherited is not None:
                    field = CollectionField(
                        field.module,
                        field.symbol,
                        field.indices,
                        inherited,
                        field.label,
                    )

            field_metadata = self._metadata(field)
            metadata.append(field_metadata)

            self._reuse_or_request(
                requests,
                reused,
                field_metadata.instance_oid,
                field,
                field_metadata.module,
                field_metadata.symbol,
                "field",
                field_metadata.syntax,
            )

            for index_ref in field_metadata.index_refs:
                self._reuse_or_request(
                    requests,
                    reused,
                    index_ref.oid,
                    field,
                    index_ref.module,
                    index_ref.symbol,
                    "index",
                    None,
                )

            if field_metadata.row_status_ref is not None:
                status_ref = field_metadata.row_status_ref
                self._reuse_or_request(
                    requests,
                    reused,
                    status_ref.oid,
                    field,
                    status_ref.module,
                    status_ref.symbol,
                    "row-status",
                    "RowStatus",
                )

        return CollectionPlan(
            metadata=tuple(metadata),
            requests=tuple(requests.values()),
            reused=tuple(reused.values()),
        )


@dataclass
class CollectionPlan:
    """A supplementary collection plan plus the metadata behind it."""

    metadata: Tuple[FieldMetadata, ...]
    requests: Tuple[CollectionRequest, ...]
    reused: Tuple[ConfirmedVariable, ...] = ()

    def __iter__(self):
        """Iterate over the requests."""
        return iter(self.requests)

    def __len__(self):
        """Return the number of requests."""
        return len(self.requests)

    @property
    def units(self) -> Dict[str, str]:
        """Return field units keyed by field key."""
        return {item.key: item.units for item in self.metadata}

    @property
    def generations(self) -> Tuple[Optional[str], ...]:
        """Return the distinct device generations in this plan."""
        seen = []
        for item in self.metadata:
            if item.generation not in seen:
                seen.append(item.generation)
        return tuple(seen)

    def prettyPrint(self):  # noqa: N802
        """Render the plan as one request per line."""
        return "\n".join(request.prettyPrint() for request in self.requests)

    def resolve(
        self, values: Optional[Iterable | Mapping] = None, generation=None
    ) -> CollectionResult:
        """Assemble rows from confirmed values plus *values*.

        Values are keyed by OID. Untagged values inherit *generation* or, when
        the plan only covers one generation, that generation. Rows are keyed
        by device generation and index values; a value read for one generation
        never fills a row belonging to another.
        """
        if generation is None and len(self.generations) == 1:
            generation = self.generations[0]

        variable_index = _group_variables(values or (), generation)

        rows: Dict[RowKey, CollectionRow] = {}

        for item in self.metadata:
            key = (item.generation, item.indices)
            row = rows.get(key)
            if row is None:
                row = CollectionRow(item.generation, item.indices)
                rows[key] = row

            variable = _find_value(
                variable_index,
                item.instance_oid,
                item.generation,
                item.syntax,
            )
            if variable is None:
                row.missing += (item.key,)
            else:
                row.values[item.key] = variable.value
            row.metadata[item.key] = item

        ordered = sorted(
            rows.values(),
            key=lambda row: (
                row.generation is not None,
                row.generation or "",
                tuple(str(x) for x in row.indices),
            ),
        )
        return CollectionResult(tuple(ordered))


def _group_variables(
    values, generation=None
) -> Dict[Oid, Tuple[ConfirmedVariable, ...]]:
    if isinstance(values, Mapping):
        values = [ConfirmedVariable(oid, value) for oid, value in values.items()]

    grouped: Dict[Oid, Tuple[ConfirmedVariable, ...]] = {}
    for variable in values:
        if not isinstance(variable, ConfirmedVariable):
            variable = ConfirmedVariable(*variable)
        if variable.generation is None and generation is not None:
            variable = ConfirmedVariable(
                variable.oid,
                variable.value,
                generation,
                variable.mib_revision,
                variable.syntax,
            )
        grouped.setdefault(variable.oid, ())
        grouped[variable.oid] += (variable,)
    return grouped


def _find_value(
    variable_index: Dict[Oid, Tuple[ConfirmedVariable, ...]],
    oid: Oid,
    generation: Optional[str],
    syntax: Optional[str],
):
    for variable in variable_index.get(oid, ()):
        if variable.generation != generation:
            continue
        if syntax and variable.syntax and variable.syntax != syntax:
            continue
        return variable
    return None


def build_collection_plan(
    mibViewController, fields, confirmed=None, generation=None, mib_revision=None
) -> CollectionPlan:
    """Build a supplementary collection plan.

    This is a convenience wrapper around :class:`CollectionPlanner`.
    """
    return CollectionPlanner(
        mibViewController,
        confirmed=confirmed,
        mib_revision=mib_revision,
        generation=generation,
    ).plan(fields, generation=generation)


plan_supplementary = build_collection_plan
