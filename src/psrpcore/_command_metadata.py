# -*- coding: utf-8 -*-

"""Offline PowerShell command metadata and parameter binding.

This module provides a small, offline model of the command metadata returned
by ``Get-Command``/``GetCommandMetadata`` and a resolver that maps an already
parsed invocation (named parameters, positional arguments, and aliases) onto
one of the command's parameter sets.

No PowerShell script is executed and no command line is parsed by this module.
A positional argument is always a literal value; a string such as
``"-OutputModule"`` supplied positionally is never re-interpreted as a
parameter name or as script syntax.
"""

import collections.abc
import dataclasses
import datetime
import enum
import hashlib
import json
import os
import typing
import uuid

_UNSET = object()
_POSITION_UNSET = -2147483648
_COMMAND_METADATA_KEYS = {
    "name",
    "commandname",
    "command",
    "cmd",
    "parameters",
    "parametermetadata",
    "parametersets",
    "sets",
    "supportsdynamicparameters",
    "metadataversion",
    "metadataversionid",
    "metadatageneration",
    "generation",
    "version",
    "commandtype",
    "namespace",
    "defaultparameterset",
    "defaultparametersetname",
}


class BindingStatus(str, enum.Enum):
    """The outcome of resolving an invocation against metadata."""

    Resolved = "resolved"
    """Exactly one parameter set accepts every input."""

    Ambiguous = "ambiguous"
    """More than one parameter set accepts every input.

    The resolver never picks the first set in this case.
    """

    AmbiguousParameter = "ambiguous_parameter"
    """A supplied parameter name matches more than one parameter."""

    MissingMandatory = "missing_mandatory"
    """No set is usable because mandatory parameters are missing."""

    TypeMismatch = "type_mismatch"
    """No set is usable because a value cannot be converted to the parameter type."""

    UnknownParameter = "unknown_parameter"
    """A named parameter is not in the metadata and dynamic parameters are ruled out."""

    Undetermined = "undetermined"
    """There is not enough evidence to choose a set, typically because a dynamic
    parameter may exist but its parameter-set membership is not known."""

    Incompatible = "incompatible"
    """No set accepts the inputs for a reason that is not a single category."""


class BindingBasis(str, enum.Enum):
    """Why an input was bound to a parameter."""

    Name = "name"
    Alias = "alias"
    Prefix = "prefix"
    Position = "position"
    RemainingArguments = "remaining_arguments"


class BindingErrorKind(str, enum.Enum):
    """The reason a candidate parameter set was rejected."""

    MissingMandatory = "missing_mandatory"
    TypeMismatch = "type_mismatch"
    ParameterNotInSet = "parameter_not_in_set"
    UnknownParameter = "unknown_parameter"
    DuplicateParameter = "duplicate_parameter"
    TooManyPositional = "too_many_positional"
    AmbiguousName = "ambiguous_name"
    InvalidMetadata = "invalid_metadata"
    MissingSetEvidence = "missing_set_evidence"
    DynamicParameter = "dynamic_parameter"


@dataclasses.dataclass(frozen=True)
class ParameterSetMetadata:
    """Per-parameter set information for a single parameter."""

    name: str
    is_mandatory: bool = False
    position: typing.Optional[int] = None
    value_from_pipeline: bool = False
    value_from_pipeline_by_property_name: bool = False
    value_from_remaining_arguments: bool = False

    @property
    def mandatory(self) -> bool:
        """Alias for :attr:`is_mandatory`."""
        return self.is_mandatory

    @property
    def IsMandatory(self) -> bool:  # noqa: N802 - mirrors .NET metadata
        return self.is_mandatory

    @property
    def Position(self) -> typing.Optional[int]:  # noqa: N802 - mirrors .NET metadata
        return self.position

    @property
    def is_positional(self) -> bool:
        """Whether the parameter can be bound by position."""
        return self.position is not None and self.position >= 0

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "IsMandatory": self.is_mandatory,
            "Position": self.position,
            "ValueFromPipeline": self.value_from_pipeline,
            "ValueFromPipelineByPropertyName": self.value_from_pipeline_by_property_name,
            "ValueFromRemainingArguments": self.value_from_remaining_arguments,
        }


@dataclasses.dataclass(frozen=True)
class CommandParameterMetadata:
    """A command parameter and its parameter set membership.

    ``parameter_sets`` maps a parameter set name to its
    :class:`ParameterSetMetadata`. An empty/``None`` mapping means the metadata
    has no evidence about parameter set membership. For a dynamic parameter
    that is an explicitly undetermined state, not an invitation to assume the
    parameter belongs to every set.

    ``default_set_metadata`` is used when the parameter is declared outside a
    particular set (for example from a top-level ``Mandatory``/``Position``).
    """

    name: str
    aliases: typing.Tuple[str, ...] = ()
    parameter_type: typing.Any = None
    parameter_sets: typing.Optional[typing.Dict[str, ParameterSetMetadata]] = None
    is_dynamic: bool = False
    switch_parameter: bool = False
    default_set_metadata: typing.Optional[ParameterSetMetadata] = None

    @property
    def type_name(self) -> typing.Optional[str]:
        """The .NET type name of the parameter, if one was provided."""
        return _type_name(self.parameter_type)

    @property
    def Name(self) -> str:  # noqa: N802 - mirrors .NET metadata
        return self.name

    @property
    def Aliases(self) -> typing.Tuple[str, ...]:  # noqa: N802 - mirrors .NET metadata
        return self.aliases

    @property
    def ParameterType(self) -> typing.Any:  # noqa: N802 - mirrors .NET metadata
        return self.parameter_type

    @property
    def ParameterSets(self) -> typing.Dict[str, "ParameterSetMetadata"]:  # noqa: N802 - mirrors .NET metadata
        return self.parameter_sets or {}

    @property
    def parameter_set_names(self) -> typing.Tuple[str, ...]:
        return tuple(self.parameter_sets or ())

    @property
    def is_mandatory(self) -> bool:
        """Whether the parameter is mandatory in any parameter set."""
        sets = list((self.parameter_sets or {}).values())
        if self.default_set_metadata is not None:
            sets.append(self.default_set_metadata)
        return any(p.is_mandatory for p in sets)

    def set_metadata(self, set_name: str) -> typing.Optional[ParameterSetMetadata]:
        if not self.parameter_sets:
            return self.default_set_metadata
        return self.parameter_sets.get(set_name)

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "Name": self.name,
            "Aliases": list(self.aliases),
            "ParameterType": self.type_name,
            "ParameterSets": {name: ps.to_dict() for name, ps in (self.parameter_sets or {}).items()},
            "default_set_metadata": self.default_set_metadata.to_dict() if self.default_set_metadata else None,
            "IsDynamic": self.is_dynamic,
            "SwitchParameter": self.switch_parameter,
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} aliases={list(self.aliases)!r} "
            f"parameter_type={self.type_name!r} is_dynamic={self.is_dynamic}>"
        )


class CommandMetadata:
    """Offline metadata for a single command.

    Args:
        name: The command name.
        parameters: A mapping or iterable of parameter metadata.
        parameter_sets: Explicit parameter set names. If omitted they are
            derived from the parameters.
        supports_dynamic_parameters: Whether the command can add dynamic
            parameters. ``None`` means there is no evidence either way.
        metadata_version: The server metadata version. If omitted a stable
            content fingerprint is used so changed metadata can still be
            detected.
        command_type: Optional command type, for example ``"Cmdlet"``.
        namespace: Optional command namespace.
    """

    def __init__(
        self,
        name: str,
        parameters: typing.Union[
            typing.Mapping[str, typing.Any],
            typing.Iterable[typing.Union["CommandParameterMetadata", typing.Mapping[str, typing.Any]]],
        ] = (),
        *,
        parameter_sets: typing.Optional[typing.Iterable[str]] = None,
        supports_dynamic_parameters: typing.Optional[bool] = None,
        metadata_version: typing.Optional[str] = None,
        command_type: typing.Optional[str] = None,
        namespace: typing.Optional[str] = None,
        default_parameter_set: typing.Optional[str] = None,
    ) -> None:
        self.name = name
        self.command_type = command_type
        self.namespace = namespace
        self.supports_dynamic_parameters = supports_dynamic_parameters
        self.default_parameter_set = default_parameter_set

        self.parameters: typing.Dict[str, CommandParameterMetadata] = {}
        for parameter in _iter_parameters(parameters):
            self.parameters[parameter.name] = parameter

        self._explicit_parameter_sets = tuple(parameter_sets) if parameter_sets is not None else None
        self._metadata_version = metadata_version
        self._metadata_fingerprint = _fingerprint(self._canonical_metadata())

    @property
    def parameter_sets(self) -> typing.Tuple[str, ...]:
        """All parameter set names in metadata order, excluding ``__AllParameterSets``."""
        if self._explicit_parameter_sets is not None:
            return _normalise_set_names(self._explicit_parameter_sets)

        return _normalise_set_names(
            name for parameter in self.parameters.values() for name in (parameter.parameter_sets or {})
        )

    @property
    def Name(self) -> str:  # noqa: N802 - mirrors .NET metadata
        return self.name

    @property
    def Parameters(self) -> typing.Dict[str, CommandParameterMetadata]:  # noqa: N802 - mirrors .NET metadata
        return self.parameters

    @property
    def ParameterSets(self) -> typing.Tuple[str, ...]:  # noqa: N802 - mirrors .NET metadata
        return self.parameter_sets

    @property
    def DefaultParameterSet(self) -> typing.Optional[str]:  # noqa: N802 - mirrors .NET metadata
        return self.default_parameter_set

    @property
    def SupportsDynamicParameters(self) -> typing.Optional[bool]:  # noqa: N802 - mirrors .NET metadata
        return self.supports_dynamic_parameters

    @property
    def MetadataVersion(self) -> str:  # noqa: N802 - mirrors .NET metadata
        return self.metadata_version

    @property
    def metadata_version(self) -> str:
        """The explicit metadata version or a content fingerprint."""
        return self._metadata_version or f"sha256:{self._metadata_fingerprint}"

    @property
    def metadata_fingerprint(self) -> str:
        """A stable fingerprint of the normalised metadata."""
        return self._metadata_fingerprint

    @property
    def version(self) -> str:
        """Alias for :attr:`metadata_version`."""
        return self.metadata_version

    def with_version(self, metadata_version: typing.Optional[str]) -> "CommandMetadata":
        """Return a copy of this metadata with a different explicit version."""
        return CommandMetadata(
            self.name,
            self.parameters.values(),
            parameter_sets=self._explicit_parameter_sets,
            supports_dynamic_parameters=self.supports_dynamic_parameters,
            metadata_version=metadata_version,
            command_type=self.command_type,
            namespace=self.namespace,
            default_parameter_set=self.default_parameter_set,
        )

    def _canonical_metadata(self) -> typing.Dict[str, typing.Any]:
        return {
            "name": self.name,
            "command_type": self.command_type,
            "namespace": self.namespace,
            "supports_dynamic_parameters": self.supports_dynamic_parameters,
            "default_parameter_set": self.default_parameter_set,
            "parameter_sets": list(self.parameter_sets),
            "parameters": {
                name: parameter.to_dict() for name, parameter in sorted(self.parameters.items(), key=lambda i: i[0])
            },
        }

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        """Serialise the metadata to a plain dictionary."""
        data = self._canonical_metadata()
        data["metadata_version"] = self.metadata_version
        return data

    def get_parameter(self, name: str) -> typing.Optional[CommandParameterMetadata]:
        """Get a parameter by canonical name (case-insensitive)."""
        key = name.lstrip("-").casefold()
        for parameter in self.parameters.values():
            if parameter.name.casefold() == key:
                return parameter

        return None

    def match_parameter(
        self,
        name: str,
        *,
        allow_prefix: bool = True,
    ) -> typing.Tuple[
        typing.Optional[CommandParameterMetadata], typing.Optional[BindingBasis], typing.Optional[str], typing.List[str]
    ]:
        """Match a supplied name to a parameter.

        Returns a tuple of ``(parameter, basis, matched_name, ambiguous_names)``.
        A supplied name may be a canonical parameter name, an alias, or an
        unambiguous parameter-name prefix. A leading ``-`` is accepted here
        because the caller has already declared this input as named; it is
        never stripped from a positional value.

        Full parameter names always take precedence over aliases. A prefix
        match against full names is considered only when neither a full name
        nor an alias matched exactly.
        """
        key = name.lstrip("-").casefold()
        if not key:
            return None, None, None, []

        exact_name: typing.List[CommandParameterMetadata] = []
        exact_alias: typing.List[typing.Tuple[CommandParameterMetadata, str]] = []
        prefix_matches: typing.Dict[str, CommandParameterMetadata] = {}

        for parameter in self.parameters.values():
            canonical = parameter.name.casefold()
            if canonical == key:
                exact_name.append(parameter)

            elif any(alias.casefold() == key for alias in parameter.aliases if alias):
                for alias in parameter.aliases:
                    if alias.casefold() == key:
                        exact_alias.append((parameter, alias))

            if allow_prefix and canonical.startswith(key):
                prefix_matches[parameter.name] = parameter

        if len(exact_name) == 1:
            return exact_name[0], BindingBasis.Name, exact_name[0].name, []

        if len(exact_name) > 1:
            return None, None, None, [p.name for p in exact_name]

        unique_alias = {p.name: (p, a) for p, a in exact_alias}
        if len(unique_alias) == 1:
            parameter, alias = next(iter(unique_alias.values()))
            return parameter, BindingBasis.Alias, alias, []

        if len(unique_alias) > 1:
            return None, None, None, sorted(unique_alias)

        if allow_prefix and len(prefix_matches) == 1:
            parameter = next(iter(prefix_matches.values()))
            return parameter, BindingBasis.Prefix, parameter.name, []

        if allow_prefix and len(prefix_matches) > 1:
            return None, None, None, sorted(prefix_matches)

        return None, None, None, []

    @classmethod
    def from_mapping(
        cls,
        value: typing.Mapping[str, typing.Any],
        *,
        name: typing.Optional[str] = None,
        metadata_version: typing.Optional[str] = None,
    ) -> "CommandMetadata":
        """Create metadata from a dictionary/JSON-like structure.

        Both the PSRP/``Get-Command`` spelling (``Parameters``/``Name``) and
        snake_case spellings are accepted.
        """
        cmd_name = name or _get(value, "Name", "CommandName", "Command", "Cmd")
        if cmd_name is None:
            raise ValueError("Command metadata does not contain a command name")

        raw_parameters = _get(value, "Parameters", "ParameterMetadata")
        if raw_parameters is None:
            # Be forgiving when the caller passes just the parameter mapping.
            raw_parameters = {k: v for k, v in value.items() if k not in _COMMAND_METADATA_KEYS}

        parameter_sets = _get(value, "ParameterSets", "Sets")
        if isinstance(parameter_sets, collections.abc.Mapping):
            parameter_sets = list(parameter_sets)
        elif isinstance(parameter_sets, str):
            parameter_sets = [parameter_sets]

        return cls(
            str(cmd_name),
            raw_parameters if raw_parameters is not None else (),
            parameter_sets=typing.cast(typing.Optional[typing.Iterable[str]], parameter_sets),
            supports_dynamic_parameters=_optional_bool(
                _get(value, "SupportsDynamicParameters", "supports_dynamic_parameters")
            ),
            metadata_version=metadata_version
            or typing.cast(
                typing.Optional[str],
                _get(value, "MetadataVersion", "MetadataVersionId", "MetadataGeneration", "Generation", "Version"),
            ),
            command_type=typing.cast(typing.Optional[str], _get(value, "CommandType")),
            namespace=typing.cast(typing.Optional[str], _get(value, "Namespace")),
            default_parameter_set=typing.cast(
                typing.Optional[str],
                _get(value, "DefaultParameterSet", "DefaultParameterSetName", "default_parameter_set"),
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "CommandMetadata":
        """Create metadata from JSON text."""
        return cls.from_mapping(typing.cast(typing.Mapping[str, typing.Any], json.loads(value)))

    @classmethod
    def from_ps_object(
        cls,
        value: typing.Any,
        *,
        name: typing.Optional[str] = None,
        metadata_version: typing.Optional[str] = None,
    ) -> "CommandMetadata":
        """Create metadata from a ``PSObject``/``PSCustomObject``-like object."""
        return cls.from_mapping(
            typing.cast(typing.Mapping[str, typing.Any], _PSObjectMapping(value)),
            name=name,
            metadata_version=metadata_version,
        )

    def with_metadata_version(self, metadata_version: typing.Optional[str]) -> "CommandMetadata":
        """Alias for :meth:`with_version`."""
        return self.with_version(metadata_version)

    def validate_version(self, metadata_version: typing.Optional[str]) -> bool:
        """Whether a recorded version still matches this metadata."""
        if metadata_version is None:
            return False

        return metadata_version == self.metadata_version

    def require_version(self, metadata_version: typing.Optional[str]) -> None:
        """Raise if a recorded version no longer matches this metadata."""
        if not self.validate_version(metadata_version):
            raise ValueError(
                f"Command metadata for {self.name!r} changed: expected version "
                f"{metadata_version!r}, current version is {self.metadata_version!r}"
            )

    @property
    def default(self) -> typing.Optional[str]:
        """The metadata-declared default parameter set, if any.

        This is descriptive only. The resolver never silently falls back to
        it when several sets are valid.
        """
        return self.default_parameter_set

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} parameters={len(self.parameters)} "
            f"parameter_sets={list(self.parameter_sets)!r} metadata_version={self.metadata_version!r}>"
        )


@dataclasses.dataclass(frozen=True)
class ParameterInput:
    """An invocation input exactly as supplied.

    ``name`` is the original parameter name/alias text for a named input, or
    ``None`` for a positional input. The ``value`` is never parsed or
    rewritten. In particular, a positional string is treated as a literal even
    if it starts with ``-`` or contains command syntax characters.
    """

    value: typing.Any
    name: typing.Optional[str] = None
    position: typing.Optional[int] = None
    index: typing.Optional[int] = None

    @property
    def is_named(self) -> bool:
        return self.name is not None

    @classmethod
    def named(cls, name: str, value: typing.Any, *, index: typing.Optional[int] = None) -> "ParameterInput":
        return cls(value=value, name=name, index=index)

    @classmethod
    def positional(cls, value: typing.Any, *, index: typing.Optional[int] = None) -> "ParameterInput":
        return cls(value=value, index=index)

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "index": self.index,
            "name": self.name,
            "position": self.position,
            "value": self.value,
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} index={self.index!r} name={self.name!r} position={self.position!r} "
            f"value={self.value!r}>"
        )


@dataclasses.dataclass(frozen=True)
class ParameterBinding:
    """A single input bound to a parameter in a candidate set."""

    input: ParameterInput
    parameter: CommandParameterMetadata
    parameter_set: str
    basis: BindingBasis
    matched_name: str
    position: typing.Optional[int] = None
    alias: typing.Optional[str] = None

    @property
    def name(self) -> str:
        """The canonical parameter name."""
        return self.parameter.name

    @property
    def value(self) -> typing.Any:
        """The original input value."""
        return self.input.value

    @property
    def reason(self) -> str:
        """A human-readable explanation of the binding basis."""
        if self.basis == BindingBasis.RemainingArguments:
            return f"collected as remaining arguments by parameter {self.parameter.name!r}"

        if self.basis == BindingBasis.Position:
            return f"bound by position {self.position}"

        if self.basis == BindingBasis.Alias:
            return f"alias {self.alias!r} resolved to parameter {self.parameter.name!r}"

        if self.basis == BindingBasis.Prefix:
            return f"parameter name prefix {self.matched_name!r} resolved to {self.parameter.name!r}"

        return f"parameter name {self.parameter.name!r}"

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "input": self.input.to_dict(),
            "parameter": self.parameter.name,
            "parameter_type": self.parameter.type_name,
            "parameter_set": self.parameter_set,
            "basis": self.basis.value,
            "matched_name": self.matched_name,
            "alias": self.alias,
            "position": self.position,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} parameter={self.parameter.name!r} basis={self.basis.value!r} "
            f"value={self.input.value!r}>"
        )


@dataclasses.dataclass(frozen=True)
class BindingFailure:
    """A reason a parameter set candidate was rejected or remains unresolved."""

    kind: BindingErrorKind
    message: str
    parameter: typing.Optional[str] = None
    input: typing.Optional[ParameterInput] = None
    parameter_set: typing.Optional[str] = None

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "parameter": self.parameter,
            "input": self.input.to_dict() if self.input else None,
            "parameter_set": self.parameter_set,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} kind={self.kind.value!r} message={self.message!r}>"


class ParameterSetCandidate:
    """The result of evaluating all inputs against one parameter set."""

    def __init__(
        self,
        name: str,
        *,
        bindings: typing.Iterable[ParameterBinding] = (),
        missing_mandatory: typing.Iterable[str] = (),
        type_mismatches: typing.Iterable[BindingFailure] = (),
        failures: typing.Iterable[BindingFailure] = (),
        unresolved: typing.Iterable[BindingFailure] = (),
    ) -> None:
        self.name = name
        self.bindings = tuple(bindings)
        self.missing_mandatory = tuple(missing_mandatory)
        self.type_mismatches = tuple(type_mismatches)
        self.failures = tuple(failures)
        self.unresolved = tuple(unresolved)

    @property
    def parameter_set(self) -> str:
        return self.name

    @property
    def eligible(self) -> bool:
        """Whether the set accepts every known input with all mandatory values."""
        return self.fully_bindable

    @property
    def structurally_accepted(self) -> bool:
        """Whether every input can be bound somewhere in this set.

        This ignores missing mandatory parameters and type conversion, and is
        useful for reporting "this set could take these names" separately from
        "this set would actually bind successfully".
        """
        structural_kinds = {
            BindingErrorKind.ParameterNotInSet,
            BindingErrorKind.UnknownParameter,
            BindingErrorKind.DuplicateParameter,
            BindingErrorKind.TooManyPositional,
            BindingErrorKind.AmbiguousName,
        }
        return not any(f.kind in structural_kinds for f in self.failures) and not self.unresolved

    @property
    def fully_bindable(self) -> bool:
        """Whether the set passes structural, mandatory, and type checks."""
        return self.structurally_accepted and not self.missing_mandatory and not self.type_mismatches

    @property
    def is_viable(self) -> bool:
        return self.eligible

    def binding_for(self, name: str) -> typing.Optional[ParameterBinding]:
        key = name.lstrip("-").casefold()
        for binding in self.bindings:
            if binding.parameter.name.casefold() == key:
                return binding

        return None

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        return {
            "parameter_set": self.name,
            "eligible": self.eligible,
            "structurally_accepted": self.structurally_accepted,
            "fully_bindable": self.fully_bindable,
            "bindings": [b.to_dict() for b in self.bindings],
            "missing_mandatory": list(self.missing_mandatory),
            "type_mismatches": [f.to_dict() for f in self.type_mismatches],
            "failures": [f.to_dict() for f in self.failures],
            "unresolved": [f.to_dict() for f in self.unresolved],
        }

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} parameter_set={self.name!r} eligible={self.eligible} "
            f"missing_mandatory={list(self.missing_mandatory)!r}>"
        )


class ParameterBindingPlan:
    """The auditable result of resolving an invocation.

    The plan keeps the original :class:`ParameterInput` objects and the basis
    for every binding. If more than one parameter set is valid it reports
    :attr:`BindingStatus.Ambiguous`; :attr:`parameter_set` remains ``None`` so
    callers cannot accidentally execute the first set.
    """

    def __init__(
        self,
        command_name: str,
        inputs: typing.Iterable[ParameterInput],
        *,
        status: BindingStatus,
        candidates: typing.Iterable[ParameterSetCandidate],
        metadata_version: typing.Optional[str],
        metadata_fingerprint: typing.Optional[str] = None,
        allow_prefix: bool = True,
        has_pipeline_input: bool = False,
        unresolved: typing.Iterable[BindingFailure] = (),
        detail: typing.Optional[str] = None,
    ) -> None:
        self.command_name = command_name
        self.inputs = tuple(inputs)
        self.status = status
        self.candidates = tuple(candidates)
        self.metadata_version = metadata_version
        self.metadata_fingerprint = metadata_fingerprint
        self.allow_prefix = allow_prefix
        self.has_pipeline_input = has_pipeline_input
        self.unresolved = tuple(unresolved)
        self.detail = detail

        self.previous_metadata_version: typing.Optional[str] = None
        self.metadata_changed = False
        self._metadata: typing.Optional[CommandMetadata] = None

    def _remember_metadata(self, metadata: CommandMetadata) -> None:
        self._metadata = metadata

    @property
    def selected(self) -> typing.Optional[ParameterSetCandidate]:
        """The uniquely selected set, or ``None`` when unresolved/ambiguous."""
        if self.status != BindingStatus.Resolved:
            return None

        eligible = [c for c in self.candidates if c.fully_bindable]
        return eligible[0] if len(eligible) == 1 else None

    @property
    def parameter_set(self) -> typing.Optional[str]:
        """The selected parameter set name, never the first valid set."""
        selected = self.selected
        return selected.name if selected else None

    @property
    def parameter_set_name(self) -> typing.Optional[str]:
        return self.parameter_set

    @property
    def candidate_sets(self) -> typing.Tuple[str, ...]:
        """All sets that fully bind every input."""
        return tuple(c.name for c in self.candidates if c.fully_bindable)

    @property
    def possible_parameter_sets(self) -> typing.Tuple[str, ...]:
        """Alias for :attr:`candidate_sets`."""
        return self.candidate_sets

    @property
    def structurally_accepted_sets(self) -> typing.Tuple[str, ...]:
        """Sets that can bind every input, regardless of mandatory/type checks."""
        return tuple(c.name for c in self.candidates if c.structurally_accepted)

    @property
    def sets_missing_mandatory(self) -> typing.Tuple[str, ...]:
        """Sets rejected only because mandatory parameters were not supplied."""
        return tuple(c.name for c in self.candidates if c.missing_mandatory)

    @property
    def sets_with_type_mismatch(self) -> typing.Tuple[str, ...]:
        """Sets rejected because at least one supplied value has the wrong type."""
        return tuple(c.name for c in self.candidates if c.type_mismatches)

    @property
    def bindings(self) -> typing.Tuple[ParameterBinding, ...]:
        """Bindings from the selected set, or an empty tuple when not resolved."""
        selected = self.selected
        return selected.bindings if selected else ()

    @property
    def missing_mandatory(self) -> typing.Tuple[str, ...]:
        """Mandatory parameter names missing from every still-relevant set.

        A uniquely resolved plan has no missing mandatory parameters, so this
        only reports mandatory names for unresolved or rejected candidates.
        """
        if self.status == BindingStatus.Resolved:
            return ()

        values: typing.List[str] = []
        for candidate in self.candidates:
            for name in candidate.missing_mandatory:
                if name not in values:
                    values.append(name)
        return tuple(values)

    @property
    def type_mismatches(self) -> typing.Tuple[BindingFailure, ...]:
        if self.status == BindingStatus.Resolved:
            return ()

        values: typing.List[BindingFailure] = []
        for candidate in self.candidates:
            for failure in candidate.type_mismatches:
                if failure not in values:
                    values.append(failure)
        return tuple(values)

    @property
    def unknown_parameters(self) -> typing.Tuple[str, ...]:
        names: typing.List[str] = []
        for failure in self.unresolved:
            if failure.kind in (BindingErrorKind.UnknownParameter, BindingErrorKind.DynamicParameter):
                if failure.parameter and failure.parameter not in names:
                    names.append(failure.parameter)
        for candidate in self.candidates:
            for failure in candidate.failures:
                if failure.kind == BindingErrorKind.UnknownParameter and failure.parameter:
                    if failure.parameter not in names:
                        names.append(failure.parameter)
        return tuple(names)

    @property
    def ambiguous_parameter_names(self) -> typing.Tuple[str, ...]:
        """Supplied names that matched more than one parameter."""
        names: typing.List[str] = []
        for candidate in self.candidates:
            for failure in candidate.failures:
                if failure.kind == BindingErrorKind.AmbiguousName and failure.parameter:
                    if failure.parameter not in names:
                        names.append(failure.parameter)
        return tuple(names)

    @property
    def is_resolved(self) -> bool:
        return self.status == BindingStatus.Resolved

    @property
    def is_ambiguous(self) -> bool:
        return self.status == BindingStatus.Ambiguous

    @property
    def is_undetermined(self) -> bool:
        return self.status == BindingStatus.Undetermined

    @property
    def needs_metadata_refresh(self) -> bool:
        return self.metadata_changed

    def binding_for(self, name: str) -> typing.Optional[ParameterBinding]:
        """Get the binding for a parameter by canonical name."""
        for binding in self.bindings:
            if binding.parameter.name.casefold() == name.lstrip("-").casefold():
                return binding

        return None

    def is_stale(self, metadata: typing.Union[str, "CommandMetadata"]) -> bool:
        """Whether this plan was produced from different metadata.

        A version mismatch is always stale. When the caller supplies metadata
        rather than a version string the content fingerprint is checked too.
        """
        if isinstance(metadata, str):
            return self.metadata_version != metadata

        if metadata.metadata_version != self.metadata_version:
            return True

        if self.metadata_fingerprint and metadata.metadata_fingerprint != self.metadata_fingerprint:
            return True

        return False

    def revalidate(self, metadata: typing.Union[str, "CommandMetadata"]) -> "ParameterBindingPlan":
        """Rebuild this plan against new server metadata.

        The original invocation inputs are reused as-is, so the revalidated
        plan keeps the same audit trail and does not re-parse positional
        strings. Supplying only a version string cannot prove the parameter
        metadata still matches, so that case stays undetermined until the
        caller provides the refreshed metadata content.
        """
        if isinstance(metadata, str):
            new_plan = ParameterBindingPlan(
                self.command_name,
                self.inputs,
                status=BindingStatus.Undetermined,
                candidates=(),
                metadata_version=metadata,
                metadata_fingerprint=None,
                allow_prefix=self.allow_prefix,
                has_pipeline_input=self.has_pipeline_input,
                detail=(
                    "server metadata version changed but refreshed command metadata "
                    "was not supplied; revalidation is required"
                ),
            )
            new_plan.metadata_changed = True

        else:
            new_plan = resolve_parameter_set(
                metadata,
                self.inputs,
                allow_prefix=self.allow_prefix,
                has_pipeline_input=self.has_pipeline_input,
            )
            new_plan.metadata_changed = self.is_stale(metadata)

        new_plan.previous_metadata_version = self.metadata_version
        return new_plan

    def to_dict(self) -> typing.Dict[str, typing.Any]:
        """Return an audit-friendly plain dictionary.

        ``inputs`` contains the original values, not their string form. Use
        :meth:`to_json` when a JSON-safe representation is needed.
        """
        return {
            "command_name": self.command_name,
            "status": self.status.value,
            "parameter_set": self.parameter_set,
            "candidate_sets": list(self.candidate_sets),
            "structurally_accepted_sets": list(self.structurally_accepted_sets),
            "missing_mandatory": list(self.missing_mandatory),
            "type_mismatches": [f.to_dict() for f in self.type_mismatches],
            "metadata_version": self.metadata_version,
            "metadata_fingerprint": self.metadata_fingerprint,
            "previous_metadata_version": self.previous_metadata_version,
            "metadata_changed": self.metadata_changed,
            "allow_prefix": self.allow_prefix,
            "inputs": [i.to_dict() for i in self.inputs],
            "bindings": [b.to_dict() for b in self.bindings],
            "candidates": [c.to_dict() for c in self.candidates],
            "unresolved": [f.to_dict() for f in self.unresolved],
            "detail": self.detail,
        }

    def to_json(self, **kwargs: typing.Any) -> str:
        """Serialise the plan to JSON.

        Values that cannot be encoded as JSON are represented with ``repr`` so
        the original object remains available on the plan itself.
        """
        kwargs.setdefault("default", _json_default)
        return json.dumps(self.to_dict(), **kwargs)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} command_name={self.command_name!r} status={self.status.value!r} "
            f"parameter_set={self.parameter_set!r} candidate_sets={list(self.candidate_sets)!r}>"
        )


# Descriptive aliases for callers that think in terms of a resolution result.
ParameterSetResolution = ParameterBindingPlan
BindingPlan = ParameterBindingPlan
BindingResult = ParameterBindingPlan


class ParameterBinder:
    """Resolve invocations for one command's metadata.

    Args:
        metadata: A :class:`CommandMetadata`, mapping, ``PSObject``, JSON
            string, or path to a JSON metadata file.
        allow_prefix: Whether an unambiguous parameter name prefix is allowed.
    """

    def __init__(
        self,
        metadata: typing.Any,
        *,
        allow_prefix: bool = True,
    ) -> None:
        self.metadata = load_command_metadata(metadata)
        self.allow_prefix = allow_prefix

    def bind(
        self,
        arguments: typing.Any = None,
        *,
        metadata_version: typing.Optional[str] = None,
        has_pipeline_input: bool = False,
    ) -> ParameterBindingPlan:
        return resolve_parameter_set(
            self.metadata,
            arguments,
            metadata_version=metadata_version,
            allow_prefix=self.allow_prefix,
            has_pipeline_input=has_pipeline_input,
        )

    resolve = bind

    def revalidate(self, plan: ParameterBindingPlan) -> ParameterBindingPlan:
        return plan.revalidate(self.metadata)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} metadata={self.metadata!r}>"


class CommandBindingCache:
    """Keep plans so all related invocations can be revalidated on version changes."""

    def __init__(self) -> None:
        self._plans: typing.List[ParameterBindingPlan] = []

    @property
    def plans(self) -> typing.Tuple[ParameterBindingPlan, ...]:
        return tuple(self._plans)

    @property
    def latest(self) -> typing.Tuple[ParameterBindingPlan, ...]:
        """The most recent plan for each recorded invocation."""
        latest: typing.List[ParameterBindingPlan] = []
        seen: typing.Set[typing.Tuple[str, str]] = set()
        for plan in reversed(self._plans):
            identity = (plan.command_name.casefold(), _plan_identity(plan))
            if identity in seen:
                continue
            seen.add(identity)
            latest.append(plan)

        latest.reverse()
        return tuple(latest)

    def record(self, plan: ParameterBindingPlan) -> ParameterBindingPlan:
        self._plans.append(plan)
        return plan

    def plans_for(self, command_name: str) -> typing.Tuple[ParameterBindingPlan, ...]:
        key = command_name.casefold()
        return tuple(p for p in self._plans if p.command_name.casefold() == key)

    def stale_plans(
        self,
        metadata: typing.Union[str, CommandMetadata],
        command_name: typing.Optional[str] = None,
    ) -> typing.Tuple[ParameterBindingPlan, ...]:
        if command_name is None and isinstance(metadata, CommandMetadata):
            command_name = metadata.name

        plans = self.plans if command_name is None else self.plans_for(command_name)
        return tuple(p for p in plans if p.is_stale(metadata))

    def revalidate(
        self,
        metadata: typing.Union[str, CommandMetadata],
        command_name: typing.Optional[str] = None,
    ) -> typing.Tuple[ParameterBindingPlan, ...]:
        """Revalidate the latest call for each affected invocation.

        Returns the new plans; the original plans are retained in the cache so
        old audit records are not silently overwritten. Revalidating the same
        command repeatedly does not multiply older revalidations.
        """
        if command_name is None and isinstance(metadata, CommandMetadata):
            command_name = metadata.name

        if command_name is None and isinstance(metadata, str):
            # A version-only refresh cannot be tied to one command, so every
            # latest plan is revalidated conservatively.
            plans = self.latest
        else:
            plans = self.plans_for(command_name or "")

        updated: typing.List[ParameterBindingPlan] = []
        seen: typing.Set[typing.Tuple[str, str]] = set()
        for plan in reversed(plans):
            identity = (plan.command_name.casefold(), _plan_identity(plan))
            if identity in seen:
                continue
            seen.add(identity)

            new_plan = plan.revalidate(metadata)
            self._plans.append(new_plan)
            updated.append(new_plan)

        # Preserve the original recording order for a stable caller experience.
        updated.reverse()
        return tuple(updated)


# Name that reads naturally at the call site without pre-building a binder.
BindingPlanStore = CommandBindingCache


def resolve_parameter_set(
    metadata: typing.Any,
    arguments: typing.Any = None,
    *,
    metadata_version: typing.Optional[str] = None,
    allow_prefix: bool = True,
    has_pipeline_input: bool = False,
) -> ParameterBindingPlan:
    """Resolve parsed invocation inputs against offline command metadata.

    This function does not execute PowerShell, does not split a command line,
    and does not inspect positional values for parameter syntax. It is safe to
    use with arbitrary strings supplied by a collection backend.

    Args:
        metadata: Command metadata as a :class:`CommandMetadata`, mapping,
            ``PSObject``, JSON string, or JSON file path.
        arguments: A ``Command``, :class:`ParameterInput` iterable, a sequence
            of ``(name, value)`` tuples, or a mapping of named values.
            ``name=None`` denotes a positional argument.
        metadata_version: Explicit version to record instead of the metadata's
            own version/fingerprint.
        allow_prefix: Allow unambiguous parameter-name prefixes for named
            inputs. Positional values are never prefix-matched.
        has_pipeline_input: Whether pipeline input was available for mandatory
            parameters that accept pipeline input.

    Returns:
        ParameterBindingPlan: The resolution, including all candidate sets
        when the result is ambiguous or undetermined.

    Raises:
        ValueError: If the metadata does not contain a command name.
    """
    command_metadata = load_command_metadata(metadata)
    inputs = _coerce_inputs(arguments)

    effective_version = metadata_version or command_metadata.metadata_version
    all_sets = list(command_metadata.parameter_sets)
    if not all_sets:
        # A command with no set metadata still has one implicit set. Do not
        # surface PowerShell's internal __AllParameterSets name as if the
        # server had declared it.
        all_sets = ["Default"]

    unresolved: typing.List[BindingFailure] = []
    candidates: typing.List[ParameterSetCandidate] = []

    for set_name in all_sets:
        candidate = _evaluate_set(
            command_metadata,
            set_name,
            inputs,
            allow_prefix=allow_prefix,
            has_pipeline_input=has_pipeline_input,
        )
        candidates.append(candidate)
        unresolved.extend(candidate.unresolved)

    unresolved = _dedupe_failures(unresolved)
    eligible = [c for c in candidates if c.eligible]

    status: BindingStatus
    detail: typing.Optional[str] = None
    if unresolved:
        status = BindingStatus.Undetermined
        detail = "dynamic parameter evidence is incomplete; no parameter set was selected"

    elif len(eligible) == 1:
        status = BindingStatus.Resolved
        detail = f"parameter set {eligible[0].name!r} accepts all inputs"

    elif len(eligible) > 1:
        status = BindingStatus.Ambiguous
        detail = "multiple parameter sets accept all inputs; the resolver does not choose the first"

    elif candidates and all(any(f.kind == BindingErrorKind.AmbiguousName for f in c.failures) for c in candidates):
        status = BindingStatus.AmbiguousParameter
        detail = "a supplied parameter name matches more than one parameter"

    elif candidates and all(any(f.kind == BindingErrorKind.UnknownParameter for f in c.failures) for c in candidates):
        status = BindingStatus.UnknownParameter
        detail = "a named parameter is not present and dynamic parameters are disabled"

    elif candidates and all(c.type_mismatches for c in candidates):
        status = BindingStatus.TypeMismatch
        detail = "a value cannot be converted to its parameter type in every parameter set"

    elif candidates and all(c.missing_mandatory for c in candidates):
        status = BindingStatus.MissingMandatory
        detail = "mandatory parameters are missing in every parameter set"

    elif candidates:
        status = BindingStatus.Incompatible
        detail = "no parameter set accepts all inputs"

    else:
        status = BindingStatus.Incompatible
        detail = "metadata does not define any parameter sets"

    plan = ParameterBindingPlan(
        command_metadata.name,
        inputs,
        status=status,
        candidates=candidates,
        metadata_version=effective_version,
        metadata_fingerprint=command_metadata.metadata_fingerprint,
        allow_prefix=allow_prefix,
        has_pipeline_input=has_pipeline_input,
        unresolved=unresolved,
        detail=detail,
    )
    plan._remember_metadata(command_metadata)
    return plan


# Friendly aliases for callers coming from a command-parsing background.
bind_parameters = resolve_parameter_set
resolve_command_parameters = resolve_parameter_set
bind_command_parameters = resolve_parameter_set


def load_command_metadata(value: typing.Any) -> CommandMetadata:
    """Load command metadata from a supported offline representation.

    Strings are treated as JSON content first, then as a JSON file path. They
    are never executed as scripts.
    """
    if isinstance(value, CommandMetadata):
        return value

    if isinstance(value, os.PathLike):
        with open(value, "r", encoding="utf-8") as fd:
            return load_command_metadata(json.load(fd))

    if isinstance(value, str):
        try:
            return load_command_metadata(json.loads(value))
        except json.JSONDecodeError:
            with open(value, "r", encoding="utf-8") as fd:
                return load_command_metadata(json.load(fd))

    if isinstance(value, collections.abc.Mapping):
        return CommandMetadata.from_mapping(value)

    return CommandMetadata.from_ps_object(value)


def _evaluate_set(
    metadata: CommandMetadata,
    set_name: str,
    inputs: typing.Tuple[ParameterInput, ...],
    *,
    allow_prefix: bool,
    has_pipeline_input: bool,
) -> ParameterSetCandidate:
    bindings: typing.List[ParameterBinding] = []
    failures: typing.List[BindingFailure] = []
    unresolved: typing.List[BindingFailure] = []
    bound_names: typing.Set[str] = set()

    # Bind named inputs first. Positional inputs then fill the remaining slots.
    for argument in inputs:
        if argument.name is None:
            continue

        parameter, basis, matched_name, ambiguous = metadata.match_parameter(
            argument.name,
            allow_prefix=allow_prefix,
        )
        if parameter is None:
            if ambiguous:
                failures.append(
                    BindingFailure(
                        BindingErrorKind.AmbiguousName,
                        f"parameter name {argument.name!r} is ambiguous: {', '.join(ambiguous)}",
                        parameter=argument.name,
                        input=argument,
                        parameter_set=set_name,
                    )
                )

            elif metadata.supports_dynamic_parameters is False:
                failures.append(
                    BindingFailure(
                        BindingErrorKind.UnknownParameter,
                        f"parameter {argument.name!r} is not present and dynamic parameters are disabled",
                        parameter=argument.name,
                        input=argument,
                        parameter_set=set_name,
                    )
                )

            else:
                unresolved.append(
                    BindingFailure(
                        BindingErrorKind.DynamicParameter,
                        f"parameter {argument.name!r} may be dynamic but set membership is unknown",
                        parameter=argument.name,
                        input=argument,
                        parameter_set=set_name,
                    )
                )
            continue

        membership = _parameter_in_set(parameter, set_name, metadata.parameter_sets)
        if membership is None:
            unresolved.append(
                BindingFailure(
                    BindingErrorKind.MissingSetEvidence,
                    f"dynamic parameter {parameter.name!r} has no parameter set evidence",
                    parameter=parameter.name,
                    input=argument,
                    parameter_set=set_name,
                )
            )
            continue

        if not membership:
            failures.append(
                BindingFailure(
                    BindingErrorKind.ParameterNotInSet,
                    f"parameter {parameter.name!r} is not part of parameter set {set_name!r}",
                    parameter=parameter.name,
                    input=argument,
                    parameter_set=set_name,
                )
            )
            continue

        if parameter.name.casefold() in bound_names:
            failures.append(
                BindingFailure(
                    BindingErrorKind.DuplicateParameter,
                    f"parameter {parameter.name!r} was supplied more than once",
                    parameter=parameter.name,
                    input=argument,
                    parameter_set=set_name,
                )
            )
            continue

        if not _value_accepts(parameter, argument.value):
            failures.append(_type_failure(parameter, argument, set_name, argument.value))
            continue

        bound_names.add(parameter.name.casefold())
        bindings.append(
            ParameterBinding(
                input=argument,
                parameter=parameter,
                parameter_set=set_name,
                basis=basis or BindingBasis.Name,
                matched_name=matched_name or parameter.name,
                alias=matched_name if basis == BindingBasis.Alias else None,
            )
        )

    positional_cursor = 0
    for argument in inputs:
        if argument.name is not None:
            continue

        position = argument.position
        if position is None:
            position = positional_cursor
            while True:
                candidate_parameter, _, occupied = _parameter_for_position(metadata, set_name, position, bound_names)
                if candidate_parameter is None or not occupied:
                    break
                # A parameter already bound by name frees its positional slot.
                position += 1
            positional_cursor = position + 1
        else:
            positional_cursor = max(positional_cursor, position + 1)

        parameter, basis, occupied = _parameter_for_position(metadata, set_name, position, bound_names)
        if parameter is None:
            remaining = _remaining_arguments_parameter(metadata, set_name, bound_names)
            if remaining is not None:
                parameter = remaining
                basis = BindingBasis.RemainingArguments

        if parameter is None:
            if metadata.supports_dynamic_parameters is False:
                failures.append(
                    BindingFailure(
                        BindingErrorKind.TooManyPositional,
                        f"no parameter accepts positional argument {position}",
                        input=argument,
                        parameter_set=set_name,
                    )
                )

            else:
                unresolved.append(
                    BindingFailure(
                        BindingErrorKind.DynamicParameter,
                        f"positional argument {position} may be a dynamic parameter but evidence is missing",
                        input=argument,
                        parameter_set=set_name,
                    )
                )
            continue

        if occupied or (parameter.name.casefold() in bound_names and basis != BindingBasis.RemainingArguments):
            failures.append(
                BindingFailure(
                    BindingErrorKind.DuplicateParameter,
                    f"parameter {parameter.name!r} was supplied more than once",
                    parameter=parameter.name,
                    input=argument,
                    parameter_set=set_name,
                )
            )
            continue

        if not _value_accepts(parameter, argument.value):
            failures.append(_type_failure(parameter, argument, set_name, argument.value))
            continue

        if basis != BindingBasis.RemainingArguments:
            bound_names.add(parameter.name.casefold())

        bindings.append(
            ParameterBinding(
                input=argument,
                parameter=parameter,
                parameter_set=set_name,
                basis=basis or BindingBasis.Position,
                matched_name=parameter.name,
                position=None if basis == BindingBasis.RemainingArguments else position,
            )
        )

    missing: typing.List[str] = []
    for parameter in metadata.parameters.values():
        membership = _parameter_in_set(parameter, set_name, metadata.parameter_sets)
        if membership is not True:
            continue

        set_metadata = _set_metadata_for(parameter, set_name)
        if set_metadata is None or not set_metadata.is_mandatory:
            continue

        if parameter.name.casefold() in bound_names:
            continue

        if has_pipeline_input and set_metadata.value_from_pipeline:
            continue

        missing.append(parameter.name)

    type_mismatches = tuple(f for f in failures if f.kind == BindingErrorKind.TypeMismatch)

    return ParameterSetCandidate(
        set_name,
        bindings=bindings,
        missing_mandatory=missing,
        type_mismatches=type_mismatches,
        failures=failures,
        unresolved=unresolved,
    )


def _parameter_for_position(
    metadata: CommandMetadata,
    set_name: str,
    position: int,
    bound_names: typing.Set[str],
) -> typing.Tuple[typing.Optional[CommandParameterMetadata], typing.Optional[BindingBasis], bool]:
    """Find the parameter declared at a position.

    Returns ``(parameter, basis, already_bound)``. A slot occupied by a named
    input is a duplicate, not an invitation to shift the value to the next
    unbound slot.
    """
    matches = []
    occupied = False
    for parameter in metadata.parameters.values():
        membership = _parameter_in_set(parameter, set_name, metadata.parameter_sets)
        if membership is not True:
            continue

        set_metadata = _set_metadata_for(parameter, set_name)
        if set_metadata is not None and set_metadata.position == position:
            matches.append(parameter)
            if parameter.name.casefold() in bound_names:
                occupied = True

    if len(matches) == 1:
        return matches[0], BindingBasis.Position, occupied

    return None, None, False


def _remaining_arguments_parameter(
    metadata: CommandMetadata,
    set_name: str,
    bound_names: typing.Set[str],
) -> typing.Optional[CommandParameterMetadata]:
    for parameter in metadata.parameters.values():
        if parameter.name.casefold() in bound_names:
            continue

        if _parameter_in_set(parameter, set_name, metadata.parameter_sets) is not True:
            continue

        set_metadata = _set_metadata_for(parameter, set_name)
        if set_metadata is not None and set_metadata.value_from_remaining_arguments:
            return parameter

    return None


def _parameter_in_set(
    parameter: CommandParameterMetadata,
    set_name: str,
    all_set_names: typing.Iterable[str],
) -> typing.Optional[bool]:
    if not parameter.parameter_sets:
        if parameter.is_dynamic:
            return None
        return True

    if set_name in parameter.parameter_sets:
        return True

    if "__AllParameterSets" in parameter.parameter_sets:
        if set_name == "__AllParameterSets":
            return True
        # A parameter declared for all sets is a member of every named set.
        return True

    return False


def _set_metadata_for(
    parameter: CommandParameterMetadata,
    set_name: str,
) -> typing.Optional[ParameterSetMetadata]:
    if not parameter.parameter_sets:
        if parameter.is_dynamic:
            return None
        # No per-set metadata but static membership is assumed; the parameter's
        # default descriptor keeps mandatory/position values when supplied.
        return parameter.default_set_metadata or ParameterSetMetadata(set_name)

    if set_name in parameter.parameter_sets:
        return parameter.parameter_sets[set_name]

    if "__AllParameterSets" in parameter.parameter_sets:
        return parameter.parameter_sets["__AllParameterSets"]

    return None


def _type_failure(
    parameter: CommandParameterMetadata,
    argument: ParameterInput,
    set_name: str,
    value: typing.Any,
) -> BindingFailure:
    return BindingFailure(
        BindingErrorKind.TypeMismatch,
        f"value for parameter {parameter.name!r} cannot be converted to {parameter.type_name!r}",
        parameter=parameter.name,
        input=argument,
        parameter_set=set_name,
    )


def _value_accepts(parameter: CommandParameterMetadata, value: typing.Any) -> bool:
    type_value = parameter.parameter_type
    if type_value is None:
        if parameter.switch_parameter:
            return isinstance(value, bool)
        return True

    if parameter.switch_parameter:
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.casefold() in ("true", "false", "$true", "$false")
        return isinstance(value, int) and value in (0, 1)

    if isinstance(type_value, type):
        if type_value is bool:
            return isinstance(value, bool) or (
                isinstance(value, str) and value.casefold() in ("true", "false", "$true", "$false")
            )
        if type_value is str:
            return True
        if type_value in (int, float):
            if isinstance(value, bool):
                return True
            if isinstance(value, (int, float)):
                return True
            if isinstance(value, str):
                try:
                    float(value)
                    return True
                except ValueError:
                    return False
            return False
        return isinstance(value, type_value)

    type_name = _type_name(type_value)
    if not type_name:
        return True

    name = _type_name_leaf(type_name)

    if value is None:
        return True

    if name in ("object", "psobject", "psbase", "pscustomobject", "variant"):
        return True

    if name in ("switchparameter", "switch"):
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.casefold() in ("true", "false", "$true", "$false")
        return isinstance(value, int) and value in (0, 1)

    if name in ("string", "psstring"):
        return True

    if name in ("char", "pschar"):
        return isinstance(value, str) and len(value) == 1

    if name in ("boolean", "bool", "psbool"):
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.casefold() in ("true", "false", "$true", "$false")
        return isinstance(value, int) and value in (0, 1)

    numeric_names = {
        "byte",
        "psbyte",
        "sbyte",
        "pssbyte",
        "int16",
        "psint16",
        "uint16",
        "psuint16",
        "int32",
        "psint",
        "uint32",
        "psuint",
        "int64",
        "psint64",
        "uint64",
        "psuint64",
        "single",
        "pssingle",
        "double",
        "psdouble",
        "decimal",
        "psdecimal",
    }
    if name in numeric_names:
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value)
            except ValueError:
                return False
            return True
        return False

    if name in ("guid", "psguid"):
        if isinstance(value, uuid.UUID):
            return True
        if isinstance(value, str):
            try:
                uuid.UUID(value)
                return True
            except ValueError:
                return False
        return False

    if name in ("uri", "psuri"):
        return isinstance(value, str) or hasattr(value, "scheme")

    if name in ("version", "psversion"):
        if isinstance(value, str):
            return bool(value) and value[0].isdigit()
        return hasattr(value, "major")

    if name in ("datetime", "psdatetime"):
        return isinstance(value, (datetime.datetime, datetime.date)) or isinstance(value, str)

    if name in ("timespan", "psduration"):
        return isinstance(value, (datetime.timedelta, int, float)) or isinstance(value, str)

    if name in ("securestring", "pssecurestring"):
        return isinstance(value, str) or hasattr(value, "decrypt")

    if name in ("scriptblock", "psscriptblock"):
        return isinstance(value, str) or type(value).__name__ == "PSScriptBlock"

    if name in ("hashtable", "dictionary", "psdict", "psprimitivedictionary"):
        return isinstance(value, collections.abc.Mapping)

    if name in ("array", "object[]", "pslist", "arraylist", "ienumerable", "icollection", "list"):
        return True

    if name in ("pscredential",):
        if isinstance(value, collections.abc.Mapping):
            return "UserName" in value or "username" in value
        return hasattr(value, "UserName") or type(value).__name__ == "PSCredential"

    if name in ("pssession", "psrunspace"):
        # Session and runspace values are live objects; primitive strings are
        # not a valid conversion target for these types.
        return not isinstance(value, (str, bytes, int, float, bool))

    if name in ("xml", "xmldocument", "psxml"):
        return isinstance(value, str) or hasattr(value, "getroot")

    # Unknown custom .NET types: err on the side of accepting the value rather
    # than reporting a type error the offline metadata cannot justify.
    return True


def _coerce_inputs(value: typing.Any) -> typing.Tuple[ParameterInput, ...]:
    if value is None:
        return ()

    if isinstance(value, ParameterInput):
        return (value,)

    # psrpcore.Command and compatible objects.
    if hasattr(value, "parameters") and hasattr(value, "command_text"):
        raw_arguments = list(value.parameters)
        return tuple(ParameterInput(value=v, name=n, index=i) for i, (n, v) in enumerate(raw_arguments))

    if isinstance(value, collections.abc.Mapping):
        return tuple(ParameterInput(value=v, name=str(k), index=i) for i, (k, v) in enumerate(value.items()))

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        # An explicit ``(name, value)`` tuple means one named input. A list of
        # two strings remains two positional values.
        return (ParameterInput(value=value[1], name=value[0], index=0),)

    if (
        hasattr(value, "inputs")
        and not isinstance(value, (str, bytes, list, tuple, dict))
        and not isinstance(value, collections.abc.Mapping)
    ):
        return _coerce_inputs(value.inputs)

    if not isinstance(value, (str, bytes)):
        try:
            raw = list(value)
        except TypeError:
            raw = None

        if raw is not None:
            inputs: typing.List[ParameterInput] = []
            for index, item in enumerate(raw):
                if isinstance(item, ParameterInput):
                    if item.index is None:
                        item = dataclasses.replace(item, index=index)
                    inputs.append(item)

                elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                    name, item_value = item
                    inputs.append(ParameterInput(value=item_value, name=name, index=index))

                elif isinstance(item, collections.abc.Mapping):
                    name = _get(item, "Name", "name")
                    item_value = _get(item, "Value", "value", default=None)
                    position = _get(item, "Position", "position")
                    inputs.append(
                        ParameterInput(
                            value=item_value,
                            name=typing.cast(typing.Optional[str], name),
                            position=typing.cast(typing.Optional[int], position),
                            index=index,
                        )
                    )

                else:
                    inputs.append(ParameterInput(value=item, index=index))

            return tuple(inputs)

    # A bare value is one positional argument. A string here is never split
    # or interpreted as a command line.
    return (ParameterInput(value=value, index=0),)


class _PSObjectMapping(collections.abc.Mapping):
    """Read-only mapping facade for PSObject-like values."""

    def __init__(self, value: typing.Any) -> None:
        self._value = value
        names: typing.List[str] = []
        ps_object = getattr(value, "PSObject", None)
        if ps_object is not None:
            for prop in list(getattr(ps_object, "adapted_properties", [])) + list(
                getattr(ps_object, "extended_properties", [])
            ):
                name = getattr(prop, "name", None)
                if name and name not in names:
                    names.append(name)

        if isinstance(value, collections.abc.Mapping):
            names.extend(str(k) for k in value if str(k) not in names)

        self._names = names

    def __getitem__(self, key: str) -> typing.Any:
        if isinstance(self._value, collections.abc.Mapping):
            try:
                return self._value[key]
            except KeyError:
                for actual, item in self._value.items():
                    if str(actual).casefold() == key.casefold():
                        return item
                raise

        for name in self._names:
            if name.casefold() == key.casefold():
                return getattr(self._value, name)

        return getattr(self._value, key)

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def _iter_parameters(
    value: typing.Any,
) -> typing.Iterator[CommandParameterMetadata]:
    if isinstance(value, collections.abc.Mapping):
        for name, raw in value.items():
            yield _coerce_parameter(str(name), raw)

    else:
        for raw in value or ():
            if isinstance(raw, CommandParameterMetadata):
                yield raw

            elif isinstance(raw, (_PSObjectMapping, collections.abc.Mapping)) or not isinstance(raw, (str, bytes)):
                yield _coerce_parameter(None, raw)

            else:
                yield CommandParameterMetadata(str(raw))


def _coerce_parameter(name: typing.Optional[str], value: typing.Any) -> CommandParameterMetadata:
    if isinstance(value, CommandParameterMetadata):
        return value

    if isinstance(value, _PSObjectMapping):
        value = value

    param_name = name or _get(value, "Name", "ParameterName", "name")
    if param_name is None:
        raise ValueError("Parameter metadata does not contain a name")

    aliases = _as_list(_get(value, "Aliases", "Alias", "aliases"))
    parameter_type = _get(value, "ParameterType", "Type", "ParameterTypeName", "parameter_type")
    parameter_sets = _get(value, "ParameterSets", "Sets", "parameter_sets")
    is_dynamic = bool(_optional_bool(_get(value, "IsDynamic", "Dynamic", "is_dynamic")) or False)
    switch_parameter = bool(_optional_bool(_get(value, "SwitchParameter", "IsSwitch", "switch_parameter")) or False)

    # Sources that provide flat Mandatory/Position fields at the parameter
    # level mean those values apply to every declared set (or the sole set).
    default_set_metadata = None
    raw_default = _get(value, "default_set_metadata", default=None)
    if raw_default is not None:
        default_set_metadata = _coerce_set_metadata(str(param_name), raw_default)

    elif any(
        _get(value, name, default=None) is not None
        for name in (
            "IsMandatory",
            "Mandatory",
            "Position",
            "ValueFromPipeline",
            "ValueFromPipelineByPropertyName",
            "ValueFromRemainingArguments",
        )
    ):
        default_set_metadata = _coerce_set_metadata(str(param_name), value)

    set_values = _coerce_parameter_sets(parameter_sets)
    if set_values and default_set_metadata is not None:
        # Round-trip output from CommandParameterMetadata.to_dict().
        for set_name, set_value in set_values.items():
            if (
                not set_value.is_mandatory
                and set_value.position is None
                and not set_value.value_from_pipeline
                and not set_value.value_from_pipeline_by_property_name
                and not set_value.value_from_remaining_arguments
            ):
                set_values[set_name] = default_set_metadata

    return CommandParameterMetadata(
        str(param_name),
        aliases=aliases,
        parameter_type=parameter_type,
        parameter_sets=set_values,
        is_dynamic=is_dynamic,
        switch_parameter=switch_parameter,
        default_set_metadata=default_set_metadata,
    )


def _coerce_parameter_sets(
    value: typing.Any,
) -> typing.Optional[typing.Dict[str, ParameterSetMetadata]]:
    if value is None:
        return None

    if isinstance(value, str):
        return {value: ParameterSetMetadata(value)}

    if isinstance(value, collections.abc.Mapping):
        result: typing.Dict[str, ParameterSetMetadata] = {}
        for set_name, raw in value.items():
            result[str(set_name)] = _coerce_set_metadata(str(set_name), raw)
        return result

    if isinstance(value, (list, tuple, set)):
        result = {}
        for item in value:
            if isinstance(item, str):
                result[item] = ParameterSetMetadata(item)

            else:
                set_name = _get(item, "Name", "name")
                if set_name is None:
                    raise ValueError("Parameter set metadata does not contain a name")
                result[str(set_name)] = _coerce_set_metadata(str(set_name), item)
        return result

    # A PSObject/PSObject-like mapping.
    try:
        mapping = _PSObjectMapping(value)
        if len(mapping):
            return {str(k): _coerce_set_metadata(str(k), mapping[k]) for k in mapping}
    except Exception:  # pragma: no cover - defensive conversion fallback
        pass

    return None


def _coerce_set_metadata(name: str, value: typing.Any) -> ParameterSetMetadata:
    if isinstance(value, ParameterSetMetadata):
        return value

    if value is None or isinstance(value, bool):
        return ParameterSetMetadata(name, is_mandatory=bool(value))

    mandatory = _optional_bool(_get(value, "IsMandatory", "Mandatory", "is_mandatory")) or False
    position = _coerce_position(_get(value, "Position", "position"))
    value_from_pipeline = bool(_optional_bool(_get(value, "ValueFromPipeline", "value_from_pipeline")) or False)
    value_from_pipeline_by_property_name = bool(
        _optional_bool(
            _get(
                value,
                "ValueFromPipelineByPropertyName",
                "value_from_pipeline_by_property_name",
            )
        )
        or False
    )
    value_from_remaining_arguments = bool(
        _optional_bool(_get(value, "ValueFromRemainingArguments", "value_from_remaining_arguments")) or False
    )

    return ParameterSetMetadata(
        name,
        is_mandatory=mandatory,
        position=position,
        value_from_pipeline=value_from_pipeline,
        value_from_pipeline_by_property_name=value_from_pipeline_by_property_name,
        value_from_remaining_arguments=value_from_remaining_arguments,
    )


def _as_list(value: typing.Any) -> typing.Tuple[str, ...]:
    if value is None:
        return ()

    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())

    try:
        return tuple(str(v) for v in value)
    except TypeError:
        return (str(value),)


def _normalise_set_names(values: typing.Iterable[str]) -> typing.Tuple[str, ...]:
    names: typing.List[str] = []
    for value in values:
        # __AllParameterSets is PowerShell's internal marker, not a real set
        # the caller can select. A command that only declares that marker has
        # one implicit set and is handled by the resolver's Default label.
        if value and value != "__AllParameterSets" and value not in names:
            names.append(value)

    return tuple(names)


def _coerce_position(value: typing.Any) -> typing.Optional[int]:
    if value is None:
        return None

    try:
        position = int(value)
    except (TypeError, ValueError):
        return None

    if position == _POSITION_UNSET or position < 0:
        return None

    return position


def _type_name_leaf(type_name: str) -> str:
    """Normalise a .NET type name to its comparable leaf name."""
    name = type_name.strip()
    if "`" in name:
        name = name.split("`", 1)[0]
    if "[" in name:
        name = name.split("[", 1)[0]

    if "," in name:
        name = name.split(",", 1)[0]

    leaf = name.split(".")[-1].split("+")[-1]
    return leaf.casefold()


def _optional_bool(value: typing.Any) -> typing.Optional[bool]:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        lower = value.casefold()
        if lower in ("true", "$true", "1"):
            return True
        if lower in ("false", "$false", "0"):
            return False

    if isinstance(value, int):
        return bool(value)

    return bool(value)


def _type_name(value: typing.Any) -> typing.Optional[str]:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, type):
        if value.__module__ == "builtins":
            return value.__qualname__
        return value.__module__ + "." + value.__qualname__

    if isinstance(value, collections.abc.Mapping) or isinstance(value, _PSObjectMapping):
        full_name = _get(value, "FullName", "full_name")
        if full_name:
            return str(full_name)
        name = _get(value, "Name", "name")
        return str(name) if name else None

    full_name = getattr(value, "FullName", None)
    if full_name:
        return str(full_name)

    name = getattr(value, "Name", None)
    if name:
        return str(name)

    return str(value)


def _get(value: typing.Any, *names: str, default: typing.Any = _UNSET) -> typing.Any:
    if value is None:
        return None if default is _UNSET else default

    if isinstance(value, _PSObjectMapping):
        for name in names:
            try:
                return value[name]
            except (KeyError, AttributeError):
                continue
        return None if default is _UNSET else default

    if isinstance(value, collections.abc.Mapping):
        casefolded = {str(k).casefold(): k for k in value}
        for name in names:
            if name in value:
                return value[name]
            actual = casefolded.get(name.casefold())
            if actual is not None:
                return value[actual]
            underscore = name.casefold().replace("_", "")
            for key, actual in casefolded.items():
                if key.replace("_", "") == underscore:
                    return value[actual]
        return None if default is _UNSET else default

    for name in names:
        try:
            return getattr(value, name)
        except (AttributeError, KeyError):
            continue

    return None if default is _UNSET else default


def _fingerprint(value: typing.Any) -> str:
    raw = json.dumps(value, sort_keys=True, default=repr, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _dedupe_failures(
    values: typing.Iterable[BindingFailure],
) -> typing.List[BindingFailure]:
    result: typing.List[BindingFailure] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _plan_identity(plan: ParameterBindingPlan) -> str:
    return _fingerprint(
        [
            (
                item.index,
                item.name,
                item.position,
                _value_identity(item.value),
            )
            for item in plan.inputs
        ]
    )


def _value_identity(value: typing.Any) -> typing.Any:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return (type(value).__name__, value)

    return (type(value).__module__, type(value).__qualname__, id(value))


def _json_default(value: typing.Any) -> str:
    return repr(value)
