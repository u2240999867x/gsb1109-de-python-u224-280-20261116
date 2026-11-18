"""Public import surface for offline command metadata binding.

The implementation lives in :mod:`psrpcore._command_metadata`; this module
exists so callers can import from ``psrpcore.command_metadata`` without
depending on a private module name.
"""

from psrpcore._command_metadata import (
    BindingBasis,
    BindingErrorKind,
    BindingFailure,
    BindingPlan,
    BindingResult,
    BindingStatus,
    CommandBindingCache,
    CommandMetadata,
    CommandParameterMetadata,
    ParameterBinder,
    ParameterBinding,
    ParameterBindingPlan,
    ParameterInput,
    ParameterSetCandidate,
    ParameterSetMetadata,
    ParameterSetResolution,
    bind_command_parameters,
    bind_parameters,
    load_command_metadata,
    resolve_command_parameters,
    resolve_parameter_set,
)

__all__ = [
    "BindingBasis",
    "BindingErrorKind",
    "BindingFailure",
    "BindingPlan",
    "BindingResult",
    "BindingStatus",
    "CommandBindingCache",
    "CommandMetadata",
    "CommandParameterMetadata",
    "ParameterBinder",
    "ParameterBinding",
    "ParameterBindingPlan",
    "ParameterInput",
    "ParameterSetCandidate",
    "ParameterSetMetadata",
    "ParameterSetResolution",
    "bind_command_parameters",
    "bind_parameters",
    "load_command_metadata",
    "resolve_command_parameters",
    "resolve_parameter_set",
]
