#
# This file is part of pysnmp software.
#
# Copyright (c) 2026, LeXtudio Inc. <support@lextudio.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Error-isolating follow-up planning for multi-binding SNMP GET requests.

A GET request carrying many OIDs fails as a whole when an agent reports one
bad binding.  This module works out which bindings can still be read without
turning failures into invented empty values.

The planner is independent of the transport.  A caller feeds it the original
request bindings and one :class:`Receipt` per response actually received.
Every receipt keeps the binding positions of the original request, so a
follow-up request can always be traced back to the caller's input.

Three groups are kept strictly apart:

* :attr:`BindingState.CONFIRMED` -- the agent returned a usable value.
* :attr:`BindingState.ABSENT` -- the agent explicitly reported that the
  variable does not exist (``noSuchObject``/``noSuchInstance``/``endOfMibView``
  or an SNMPv1 ``noSuchName`` naming that binding).
* :attr:`BindingState.UNCONFIRMED` -- no receipt settled the binding.

:attr:`BindingState.CONFLICT` is a fourth, deliberately non-terminal group
for receipts that cannot be trusted (out-of-range ``error-index``,
request/response identity mismatch, or contradictory values).  Conflicting
bindings are reported, never silently retried or filled with an empty value.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pyasn1.type import univ

from pysnmp.proto import rfc1905
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
from pysnmp.smi.rfc1902 import ObjectType

__all__ = [
    "BindingState",
    "BindingOutcome",
    "IsolationPlan",
    "IsolationPlanner",
    "IsolationResult",
    "Receipt",
    "RetryBudget",
    "SamplingWindow",
    "SNMP_V1",
    "SNMP_V2C",
    "SNMP_V3",
    "classify_receipt",
    "error_status_name",
    "execute_isolation_plan",
    "isolation_plan",
    "isolation_plan_from_receipt",
    "merge_isolation_results",
    "sampling_windows_compatible",
    "snmp_version",
]


SNMP_V1 = 0
SNMP_V2C = 1
SNMP_V3 = 3

# SNMPv1 only knows statuses 0..5.  The v2c/v3 table adds the remaining
# values, so the same integer may mean different things depending on version.
_V1_ERRORS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
}
_V2_ERRORS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    8: "wrongLength",
    9: "wrongEncoding",
    10: "wrongValue",
    11: "noCreation",
    12: "inconsistentValue",
    13: "resourceUnavailable",
    14: "commitFailed",
    15: "undoFailed",
    16: "authorizationError",
    17: "notWritable",
    18: "inconsistentName",
}


class BindingState(str, Enum):
    """Final state of a single requested binding."""

    CONFIRMED = "confirmed"
    """The agent returned a usable value."""

    ABSENT = "absent"
    """The agent explicitly reported the variable as not existing."""

    UNCONFIRMED = "unconfirmed"
    """No receipt could settle the binding within the retry budget."""

    CONFLICT = "conflict"
    """Receipts disagree, or a receipt could not be trusted."""


@dataclass(frozen=True)
class SamplingWindow:
    """A time window in which a set of receipts was sampled.

    Windows are compatible when they overlap, i.e. when two receipts can be
    treated as describing the same sampled state.  Confirmed bindings from an
    earlier window may be reused, but their receipt is not merged into a
    later window's snapshot.
    """

    start: float
    end: float

    def __post_init__(self):
        """Validate the window."""
        if self.end < self.start:
            raise ValueError("sampling window end precedes start")

    def overlaps(self, other: "SamplingWindow") -> bool:
        """Return ``True`` when the two sampling windows overlap."""
        return self.start <= other.end and other.start <= self.end


def sampling_windows_compatible(
    first: Optional[Any], second: Optional[Any]
) -> bool:
    """Return ``True`` when two sampling-window tokens may be combined.

    ``None`` means unspecified and is compatible with anything.  Two
    :class:`SamplingWindow` instances are compared by overlap; any other
    values are compared for equality.
    """
    if first is None or second is None:
        return True
    if isinstance(first, SamplingWindow) and isinstance(second, SamplingWindow):
        return first.overlaps(second)
    return first == second


@dataclass(frozen=True)
class RetryBudget:
    """Limits for one error-isolation run.

    Parameters
    ----------
    max_requests : int
        Maximum number of GET requests in the run, including the first one.
    max_bindings_per_request : int
        Upper bound on bindings in any request.
    max_attempts_per_binding : int
        Upper bound on the number of requests a single original binding may
        appear in.
    """

    max_requests: int = 8
    max_bindings_per_request: int = 16
    max_attempts_per_binding: int = 3

    def __post_init__(self):
        """Validate the budget."""
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if self.max_bindings_per_request < 1:
            raise ValueError("max_bindings_per_request must be at least 1")
        if self.max_attempts_per_binding < 1:
            raise ValueError("max_attempts_per_binding must be at least 1")

    def with_request_size(self, request_size: Optional[int]) -> "RetryBudget":
        """Return a copy bounded by an explicit request size."""
        if request_size is None:
            return self
        if request_size < 1:
            raise ValueError("request_size must be at least 1")
        return RetryBudget(
            max_requests=self.max_requests,
            max_bindings_per_request=request_size,
            max_attempts_per_binding=self.max_attempts_per_binding,
        )


@dataclass(frozen=True)
class Receipt:
    """One response actually received for one GET request.

    Parameters
    ----------
    request_bindings : sequence
        Bindings sent in that request, in wire order.
    error_status : int or str
        PDU ``error-status`` exactly as received.
    error_index : int
        PDU ``error-index`` exactly as received (1-based; 0 means none).
    values : sequence, optional
        Per-binding values from the response PDU, in wire order.
    error_indication : object, optional
        Transport/protocol level error, for example a timeout.
    version : int, optional
        Message processing model: 0 for SNMPv1, 1 for SNMPv2c and 3 for
        SNMPv3.  When omitted, the version passed to :func:`isolation_plan`
        is used.
    positions : sequence of int, optional
        Zero-based positions of these bindings in the *original* request.
        Defaults to ``range(len(request_bindings))``.
    request_id, response_request_id : object, optional
        Request identity expected in the response and the identity actually
        observed.
    matched : bool
        ``False`` when the response identity did not match the request.
    sampling_window : object, optional
        Token identifying the sampling window.  See :class:`SamplingWindow`.
    row : int
        Follow-up round number, for audit purposes.
    """

    request_bindings: Sequence[Any]
    error_status: Any = 0
    error_index: int = 0
    values: Optional[Sequence[Any]] = None
    error_indication: Optional[Any] = None
    version: Optional[int] = None
    positions: Optional[Sequence[int]] = None
    request_id: Optional[Any] = None
    response_request_id: Optional[Any] = None
    matched: bool = True
    sampling_window: Optional[Any] = None
    row: int = 0

    @property
    def wire_positions(self) -> Tuple[int, ...]:
        """Return zero-based positions in the request as sent."""
        return tuple(range(len(self.request_bindings)))

    @property
    def original_positions(self) -> Tuple[int, ...]:
        """Return zero-based positions in the original request."""
        if self.positions is None:
            return self.wire_positions
        return tuple(int(x) for x in self.positions)

    @property
    def error_name(self) -> str:
        """Return the protocol-specific symbolic error status."""
        version = SNMP_V2C if self.version is None else int(self.version)
        return error_status_name(self.error_status, version)


@dataclass(frozen=True)
class BindingOutcome:
    """What happened to one binding from the original request."""

    position: int
    request_oid: Any
    request_binding: Any
    state: BindingState
    value: Any = None
    error_status: Optional[str] = None
    error_index: Optional[int] = None
    reason: Optional[str] = None
    receipts: Tuple[int, ...] = field(default_factory=tuple)

    @property
    def oid(self):
        """Return the OID of the requested binding."""
        return self.request_oid

    @property
    def is_confirmed(self) -> bool:
        """Return ``True`` when a usable value was obtained."""
        return self.state is BindingState.CONFIRMED

    @property
    def is_absent(self) -> bool:
        """Return ``True`` when the agent reported the variable missing."""
        return self.state is BindingState.ABSENT

    @property
    def is_unconfirmed(self) -> bool:
        """Return ``True`` when the binding is still unresolved."""
        return self.state is BindingState.UNCONFIRMED

    @property
    def is_conflict(self) -> bool:
        """Return ``True`` when the binding was held back as a conflict."""
        return self.state is BindingState.CONFLICT

    def as_varbind(self) -> Any:
        """Return the original binding carrying the observed value."""
        if not self.is_confirmed or self.value is None:
            return self.request_binding
        binding = self.request_binding
        if isinstance(binding, ObjectType):
            return binding.with_value(self.value)
        if isinstance(binding, tuple):
            return (binding[0], self.value)
        return self.value


@dataclass(frozen=True)
class IsolationPlan:
    """The next request recommended by the planner.

    ``positions`` refer to the original request, ``bindings`` are the values
    to send in that order, and ``row`` is the follow-up round number.
    """

    positions: Tuple[int, ...]
    bindings: Tuple[Any, ...]
    row: int = 0
    reason: str = ""

    def __post_init__(self):
        """Validate the plan."""
        if not self.positions:
            raise ValueError("an isolation plan must contain bindings")
        if len(self.positions) != len(self.bindings):
            raise ValueError("positions and bindings must align")

    @property
    def size(self) -> int:
        """Return the number of bindings in the plan."""
        return len(self.positions)


@dataclass
class IsolationResult:
    """Outcome of one planning round plus its audit trail."""

    original_request: Tuple[Any, ...]
    outcomes: Tuple[BindingOutcome, ...]
    plan: Optional[IsolationPlan] = None
    receipts: Tuple[Receipt, ...] = field(default_factory=tuple)
    ignored_receipts: Tuple[Receipt, ...] = field(default_factory=tuple)
    exhausted: bool = False
    budget: Optional[RetryBudget] = None
    sampling_window: Optional[Any] = None
    version: int = SNMP_V2C

    @property
    def confirmed(self) -> Tuple[BindingOutcome, ...]:
        """Bindings read successfully."""
        return tuple(x for x in self.outcomes if x.is_confirmed)

    @property
    def absent(self) -> Tuple[BindingOutcome, ...]:
        """Bindings explicitly reported as not existing."""
        return tuple(x for x in self.outcomes if x.is_absent)

    @property
    def unconfirmed(self) -> Tuple[BindingOutcome, ...]:
        """Bindings still unresolved."""
        return tuple(x for x in self.outcomes if x.is_unconfirmed)

    @property
    def conflicts(self) -> Tuple[BindingOutcome, ...]:
        """Bindings held back because receipts could not be trusted."""
        return tuple(x for x in self.outcomes if x.is_conflict)

    @property
    def settled(self) -> bool:
        """Return ``True`` when every binding is confirmed or absent."""
        return all(x.is_confirmed or x.is_absent for x in self.outcomes)

    @property
    def groups(self) -> Dict[BindingState, Tuple[BindingOutcome, ...]]:
        """Return outcomes grouped by state."""
        return {
            BindingState.CONFIRMED: self.confirmed,
            BindingState.ABSENT: self.absent,
            BindingState.UNCONFIRMED: self.unconfirmed,
            BindingState.CONFLICT: self.conflicts,
        }

    @property
    def confirmed_varbinds(self) -> Tuple[Any, ...]:
        """Return original bindings with confirmed values."""
        return tuple(x.as_varbind() for x in self.confirmed)

    @property
    def absent_varbinds(self) -> Tuple[Any, ...]:
        """Return original bindings explicitly reported as missing."""
        return tuple(x.request_binding for x in self.absent)

    @property
    def unconfirmed_varbinds(self) -> Tuple[Any, ...]:
        """Return original bindings that remain unconfirmed."""
        return tuple(x.request_binding for x in self.unconfirmed)

    @property
    def conflict_varbinds(self) -> Tuple[Any, ...]:
        """Return original bindings held back as conflicts."""
        return tuple(x.request_binding for x in self.conflicts)

    def outcome_at(self, position: int) -> BindingOutcome:
        """Return the outcome for an original binding position."""
        return self.outcomes[position]


def snmp_version(auth_data: Any, default: int = SNMP_V2C) -> int:
    """Return the message processing model of an auth object.

    Supports both the v3arch ``CommunityData``/``UsmUserData`` classes
    (``message_processing_model``) and the v1arch ``CommunityData``
    (``mpModel``).
    """
    version = getattr(auth_data, "message_processing_model", None)
    if version is None:
        version = getattr(auth_data, "mpModel", None)
    return default if version is None else int(version)


def _status_name(value: Any, version: int) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "prettyPrint"):
        try:
            name = value.prettyPrint()
        except Exception:  # pragma: no cover - defensive
            name = None
        if name and not name.lstrip("-").isdigit():
            return name
    try:
        code = int(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)
    table = _V1_ERRORS if version == SNMP_V1 else _V2_ERRORS
    return table.get(code, str(code))


def error_status_name(value: Any, version: int = SNMP_V2C) -> str:
    """Return the symbolic error-status for a specific SNMP version.

    The same numeric value can mean different things in SNMPv1 and in
    SNMPv2c/v3.  ``6`` is outside SNMPv1's table and is therefore reported
    numerically instead of being misread as ``noAccess``.
    """
    return _status_name(value, version) or "noError"


def _binding_oid(binding: Any) -> Any:
    if isinstance(binding, ObjectType):
        try:
            return binding[0]
        except Exception as exc:
            raise ValueError(
                "ObjectType bindings must be resolved with MIB before "
                "isolation planning"
            ) from exc
    if isinstance(binding, (tuple, list)):
        return binding[0]
    raise TypeError(f"Unsupported variable binding {binding!r}")


def _binding_key(binding: Any) -> Any:
    """Return a stable, hashable identity for a binding."""
    oid = _binding_oid(binding)
    if hasattr(oid, "asTuple"):
        return tuple(oid.asTuple())
    try:
        return tuple(oid)
    except TypeError:  # pragma: no cover - defensive
        return oid


def _is_exception_value(value: Any) -> bool:
    value = _unwrap_value(value)
    return getattr(value, "tagSet", None) in (
        NoSuchObject.tagSet,
        NoSuchInstance.tagSet,
        EndOfMibView.tagSet,
    )


def _unwrap_value(value: Any) -> Any:
    """Return the SNMP value part of an ObjectType, if given one."""
    if isinstance(value, ObjectType):
        try:
            return value[1]
        except Exception:  # pragma: no cover - unresolved ObjectType
            return value
    return value


def _is_absent_value(value: Any) -> bool:
    value = _unwrap_value(value)
    return getattr(value, "tagSet", None) in (
        NoSuchObject.tagSet,
        NoSuchInstance.tagSet,
        EndOfMibView.tagSet,
    )


def _is_absent_reason(value: Any) -> str:
    tag_set = getattr(_unwrap_value(value), "tagSet", None)
    if tag_set == NoSuchObject.tagSet:
        return "noSuchObject"
    if tag_set == NoSuchInstance.tagSet:
        return "noSuchInstance"
    if tag_set == EndOfMibView.tagSet:
        return "endOfMibView"
    return "no such object"


def _has_value(value: Any) -> bool:
    value = _unwrap_value(value)
    if value is None:
        return False
    if _is_exception_value(value):
        return False
    if isinstance(value, univ.Null):
        # Neither SNMPv1 nor SNMPv2c carries a usable Null in a GET
        # response; a remaining Null means the agent did not answer it.
        return False
    return getattr(value, "tagSet", None) != rfc1905.unSpecified.tagSet


def _same_value(first: Any, second: Any) -> bool:
    first, second = _unwrap_value(first), _unwrap_value(second)
    if first is second:
        return True
    try:
        if bool(first == second):
            return True
    except Exception:  # pragma: no cover - pyasn1 type mismatch
        pass
    try:
        return bool(first.prettyPrint() == second.prettyPrint())
    except Exception:  # pragma: no cover - defensive
        return False


def _receipt_version(receipt: Receipt, default: int) -> int:
    return default if receipt.version is None else int(receipt.version)


def _set_state(
    states: Dict[int, Dict[str, Any]],
    position: int,
    state: BindingState,
    **kwargs,
) -> None:
    """Record a state without ever downgrading a conflict."""
    current = states.get(position, {})
    if current.get("state") is BindingState.CONFLICT:
        return
    if current.get("state") is state:
        states[position] = {**current, **kwargs, "state": state}
        return
    if state is BindingState.CONFIRMED and current.get("state") is BindingState.ABSENT:
        states[position] = {
            **current,
            "state": BindingState.CONFLICT,
            "reason": "binding reported both present and absent",
        }
        return
    if state is BindingState.ABSENT and current.get("state") is BindingState.CONFIRMED:
        states[position] = {
            **current,
            "state": BindingState.CONFLICT,
            "reason": "binding reported both present and absent",
            "value": None,
        }
        return
    states[position] = {**current, **kwargs, "state": state}


def _check_receipt(
    receipt: Receipt, default_version: int
) -> Tuple[bool, str]:
    """Validate receipt fields that do not depend on binding values."""
    version = _receipt_version(receipt, default_version)
    positions = receipt.original_positions
    wire_count = len(receipt.request_bindings)

    if len(positions) != wire_count:
        return False, "receipt binding positions do not align"
    if len(set(positions)) != len(positions):
        return False, "receipt contains duplicate binding positions"

    try:
        index = int(receipt.error_index or 0)
    except (TypeError, ValueError):
        return False, "error-index is not an integer"

    if index < 0 or index > wire_count:
        return False, f"error-index {index} outside 0..{wire_count}"

    if not receipt.matched:
        return False, "request/response identity mismatch"

    if (
        receipt.request_id is not None
        and receipt.response_request_id is not None
        and receipt.request_id != receipt.response_request_id
    ):
        return False, "request/response identity mismatch"

    status = error_status_name(receipt.error_status, version)
    if status == "noError" and index:
        return False, "error-index set with noError"
    if status == "tooBig" and index:
        return False, "tooBig must carry error-index 0"
    if receipt.values is not None and len(receipt.values) > wire_count:
        return False, "response carried more bindings than the request"

    return True, ""


def _apply_receipt(
    receipt: Receipt,
    positions: Sequence[int],
    states: Dict[int, Dict[str, Any]],
    receipt_number: int,
    default_version: int,
) -> None:
    """Fold one receipt into ``states``."""
    version = _receipt_version(receipt, default_version)
    status = error_status_name(receipt.error_status, version)
    index = int(receipt.error_index or 0)
    wire_count = len(positions)

    for position in positions:
        current = states.setdefault(position, {})
        history = list(current.get("receipts", ()))
        if receipt_number not in history:
            history.append(receipt_number)
        current["receipts"] = history
        current["error_status"] = status
        current["error_index"] = index

    valid, problem = _check_receipt(receipt, default_version)
    if not valid:
        for position in positions:
            _set_state(
                states,
                position,
                BindingState.CONFLICT,
                reason=problem,
                error_status=status,
                error_index=index,
            )
        return

    if receipt.error_indication is not None:
        for position in positions:
            _set_state(
                states,
                position,
                BindingState.UNCONFIRMED,
                reason=str(receipt.error_indication),
                error_status=status,
                error_index=index,
            )
        return

    if status == "tooBig":
        # The agent discarded the whole request without answering any
        # binding; every binding stays eligible for a smaller request.
        for position in positions:
            _set_state(
                states,
                position,
                BindingState.UNCONFIRMED,
                reason="tooBig",
                error_status=status,
                error_index=index,
            )
        return

    values = list(receipt.values or ())

    if status == "noSuchName":
        if version != SNMP_V1 and index == 0:
            for position in positions:
                _set_state(
                    states,
                    position,
                    BindingState.UNCONFIRMED,
                    reason="noSuchName without error-index",
                    error_status=status,
                    error_index=index,
                )
            return

        target = index - 1 if index else None
        for wire_position, position in enumerate(positions):
            if target == wire_position:
                _set_state(
                    states,
                    position,
                    BindingState.ABSENT,
                    reason="noSuchName",
                    value=None,
                    error_status=status,
                    error_index=index,
                )
                continue
            value = values[wire_position] if wire_position < len(values) else None
            if _has_value(value):
                _remember_value(
                    states, position, value, status, index, version
                )
            else:
                _set_state(
                    states,
                    position,
                    BindingState.UNCONFIRMED,
                    reason=(
                        "noSuchName response did not carry this binding"
                        if target is not None
                        else "noSuchName without error-index"
                    ),
                    error_status=status,
                    error_index=index,
                )
        return

    if status != "noError":
        # SNMPv1 and v2c return the request bindings (without values) when
        # error-status is set.  Only error-index identifies the binding the
        # agent complained about; nothing here is an answer.
        for wire_position, position in enumerate(positions):
            is_target = index and wire_position == index - 1
            _set_state(
                states,
                position,
                BindingState.UNCONFIRMED,
                reason=(
                    status if is_target or not index
                    else f"{status} reported for another binding"
                ),
                error_status=status,
                error_index=index,
            )
        return

    if len(values) < wire_count:
        for wire_position, position in enumerate(positions):
            value = values[wire_position] if wire_position < len(values) else None
            _remember_value(
                states,
                position,
                value,
                status,
                index,
                version,
                missing_reason="response carried no value for binding",
            )
        return

    for wire_position, position in enumerate(positions):
        _remember_value(
            states,
            position,
            values[wire_position],
            status,
            index,
            version,
            missing_reason="response carried no value for binding",
        )


def _remember_value(
    states: Dict[int, Dict[str, Any]],
    position: int,
    value: Any,
    status: str,
    index: int,
    version: int = SNMP_V2C,
    missing_reason: str = "response carried no value for binding",
) -> None:
    """Record a noError value, detecting contradictions."""
    value = _unwrap_value(value)
    if version == SNMP_V1 and _is_exception_value(value):
        # SNMPv1 has no per-binding exception values.  Such a value means
        # a proxy or broken agent leaked SNMPv2 semantics, so the binding
        # cannot be trusted either way.
        _set_state(
            states,
            position,
            BindingState.CONFLICT,
            reason="SNMPv1 response carried an SNMPv2 exception value",
            error_status=status,
            error_index=index,
        )
        return
    if _is_absent_value(value):
        _set_state(
            states,
            position,
            BindingState.ABSENT,
            reason=_is_absent_reason(value),
            value=value,
            error_status=status,
            error_index=index,
        )
        return
    if not _has_value(value):
        _set_state(
            states,
            position,
            BindingState.UNCONFIRMED,
            reason=missing_reason,
            error_status=status,
            error_index=index,
        )
        return

    current = states.get(position, {})
    previous = current.get("value")
    if (
        current.get("state") is BindingState.CONFIRMED
        and previous is not None
        and not _same_value(previous, value)
    ):
        _set_state(
            states,
            position,
            BindingState.CONFLICT,
            reason="receipts returned different values",
            error_status=status,
            error_index=index,
        )
        return
    _set_state(
        states,
        position,
        BindingState.CONFIRMED,
        value=value,
        reason=None,
        error_status=status,
        error_index=index,
    )


def classify_receipt(
    request_bindings: Sequence[Any],
    receipt: Receipt,
    *,
    version: int = SNMP_V2C,
) -> Tuple[Tuple[int, BindingState, Any, str], ...]:
    """Classify one receipt without planning anything.

    Returns ``(position, state, value, reason)`` tuples for every binding in
    ``request_bindings``.  This is useful for explaining how a particular
    SNMP version interprets one response.
    """
    bindings = tuple(request_bindings)
    states: Dict[int, Dict[str, Any]] = {
        position: {} for position in range(len(bindings))
    }
    positions = receipt.original_positions
    if len(positions) != len(receipt.request_bindings):
        positions = tuple(range(len(bindings)))
    if len(positions) != len(receipt.request_bindings) or any(
        position < 0 or position >= len(bindings) for position in positions
    ):
        return tuple(
            (position, BindingState.CONFLICT, None, "invalid binding positions")
            for position in range(len(bindings))
        )

    _apply_receipt(receipt, positions, states, 0, version)
    return tuple(
        (
            position,
            states[position].get("state", BindingState.UNCONFIRMED),
            states[position].get("value"),
            states[position].get("reason", ""),
        )
        for position in range(len(bindings))
    )


def _seed_reused(
    states: Dict[int, Dict[str, Any]],
    bindings: Sequence[Any],
    reused: Sequence[BindingOutcome],
) -> None:
    """Seed outcomes carried over from a previous run."""
    for outcome in reused:
        position = outcome.position
        if position < 0 or position >= len(bindings):
            continue
        if _binding_key(bindings[position]) != _binding_key(
            outcome.request_binding
        ):
            continue
        if outcome.is_confirmed:
            states[position] = {
                "state": BindingState.CONFIRMED,
                "value": outcome.value,
                "reason": "reused confirmed binding",
                "receipts": list(outcome.receipts),
                "error_status": outcome.error_status,
                "error_index": outcome.error_index,
            }
        elif outcome.is_absent:
            states[position] = {
                "state": BindingState.ABSENT,
                "value": outcome.value,
                "reason": "reused absent binding",
                "receipts": list(outcome.receipts),
                "error_status": outcome.error_status,
                "error_index": outcome.error_index,
            }


def _compatible_receipts(
    receipts: Sequence[Receipt], sampling_window: Optional[Any]
) -> Tuple[Tuple[Receipt, ...], Tuple[Receipt, ...]]:
    """Split receipts into compatible and incompatible ones."""
    if sampling_window is None:
        return tuple(receipts), ()
    compatible = []
    ignored = []
    for receipt in receipts:
        if sampling_windows_compatible(receipt.sampling_window, sampling_window):
            compatible.append(receipt)
        else:
            ignored.append(receipt)
    return tuple(compatible), tuple(ignored)


def _last_error_target(
    receipts: Sequence[Receipt], version: int
) -> Optional[int]:
    """Return the original position named by the latest receipt.

    Only the most recent usable receipt is considered: older error-index
    values describe requests that have already been split.
    """
    for receipt in reversed(receipts):
        if receipt.error_indication is not None:
            continue
        status = error_status_name(receipt.error_status, version)
        if status == "noError":
            return None
        if status == "tooBig":
            return None
        index = int(receipt.error_index or 0)
        if not index:
            return None
        positions = receipt.original_positions
        if 0 < index <= len(positions):
            return positions[index - 1]
        return None
    return None


def _mark_untrusted_receipt(
    states: Dict[int, Dict[str, Any]],
    bindings: Sequence[Any],
    receipt: Receipt,
    positions: Sequence[int],
) -> None:
    """Hold back bindings described by a receipt whose positions are bad."""
    status = error_status_name(receipt.error_status, receipt.version or SNMP_V2C)
    valid_positions = {
        position for position in positions if 0 <= position < len(bindings)
    }
    # Without usable positions the whole receipt is suspect; the bindings it
    # names cannot be trusted, and neither can the rest of that request.
    targets = valid_positions or set(range(len(bindings)))
    for position in targets:
        _set_state(
            states,
            position,
            BindingState.CONFLICT,
            reason="receipt binding positions are invalid",
            error_status=status,
        )


def _choose_plan_positions(
    eligible: Sequence[int],
    attempts: Dict[int, int],
    sent_sets: Sequence[frozenset],
    last_status: str,
    offender: Optional[int],
    max_size: int,
) -> List[int]:
    """Pick the next subset without re-sending a failed identical request.

    Candidates are the whole pool, its halves and its singletons.  Each is
    scored by how often that exact subset was already sent, then by
    preferring multi-binding batches over singletons, then by larger size
    and accumulated attempts.  This keeps every follow-up anchored to what
    the agent actually answered instead of blindly repeating a batch.
    """
    pool = list(eligible)
    if offender in pool and len(pool) > 1:
        # Set the offending binding aside so the rest can still be read.
        pool = [position for position in pool if position != offender]
    elif offender is not None:
        pool = list(pool)

    if not pool:
        return []

    pool = pool[:max_size]
    candidates: List[List[int]] = [pool]

    if len(pool) > 1:
        middle = max(1, len(pool) // 2)
        candidates.extend([pool[:middle], pool[middle:]])

    candidates.extend([[position] for position in pool])

    unique: List[List[int]] = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = frozenset(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)

    sent_counts = [sent_sets.count(frozenset(x)) for x in unique]

    def score(index):
        candidate = unique[index]
        return (
            sent_counts[index],
            0 if len(candidate) > 1 else 1,
            -len(candidate),
            sum(attempts.get(position, 0) for position in candidate),
            candidate,
        )

    best = min(range(len(unique)), key=score)
    return unique[best]


def isolation_plan(
    request_bindings: Sequence[Any],
    receipts: Sequence[Receipt] = (),
    *,
    version: int = SNMP_V2C,
    budget: Optional[RetryBudget] = None,
    request_size: Optional[int] = None,
    sampling_window: Optional[Any] = None,
    reused: Sequence[BindingOutcome] = (),
) -> IsolationResult:
    """Interpret receipts and recommend the next isolation request.

    Parameters
    ----------
    request_bindings : sequence
        Bindings of the original GET request, in wire order.
    receipts : sequence of :class:`Receipt`
        Responses actually received for that request, in arrival order.
    version : int
        Message processing model of the request: ``0`` for SNMPv1, ``1`` for
        SNMPv2c or ``3`` for SNMPv3.
    budget : :class:`RetryBudget`, optional
        Request-size and retry limits.
    request_size : int, optional
        Maximum bindings per request; overrides the budget's
        ``max_bindings_per_request`` when both are supplied.
    sampling_window : object, optional
        When given, only receipts compatible with this window are folded
        into the result.  Incompatible receipts stay in the audit trail.
    reused : sequence of :class:`BindingOutcome`, optional
        Previously confirmed/absent outcomes to reuse instead of querying
        those bindings again.

    Returns
    -------
    :class:`IsolationResult`
        Per-binding outcomes plus the next :class:`IsolationPlan`, if any.
    """
    budget = (budget or RetryBudget()).with_request_size(request_size)
    bindings = tuple(request_bindings)
    all_receipts = tuple(receipts)

    if not bindings:
        return IsolationResult(
            original_request=(),
            outcomes=(),
            plan=None,
            receipts=all_receipts,
            exhausted=False,
            budget=budget,
            sampling_window=sampling_window,
            version=version,
        )

    states: Dict[int, Dict[str, Any]] = {
        position: {} for position in range(len(bindings))
    }
    _seed_reused(states, bindings, reused)

    compatible, ignored = _compatible_receipts(all_receipts, sampling_window)
    requests_made = 0

    for receipt_number, receipt in enumerate(compatible):
        positions = receipt.original_positions
        if len(positions) != len(receipt.request_bindings) or any(
            position < 0 or position >= len(bindings) for position in positions
        ):
            _mark_untrusted_receipt(states, bindings, receipt, positions)
            continue

        requests_made += 1
        _apply_receipt(receipt, positions, states, receipt_number, version)
    pending = [
        position
        for position in range(len(bindings))
        if states[position].get("state")
        not in (BindingState.CONFIRMED, BindingState.ABSENT, BindingState.CONFLICT)
    ]

    attempts = {
        position: len(states[position].get("receipts", ()))
        for position in range(len(bindings))
    }

    plan = None
    exhausted = False

    if pending:
        retriable = [
            position
            for position in pending
            if attempts[position] < budget.max_attempts_per_binding
        ]
        for position in pending:
            if position not in retriable:
                _set_state(
                    states,
                    position,
                    BindingState.UNCONFIRMED,
                    reason="retry budget exhausted for this binding",
                )
                exhausted = True

        if requests_made >= budget.max_requests:
            exhausted = True
            retriable = []

        if retriable:
            last_status = (
                error_status_name(compatible[-1].error_status, version)
                if compatible
                else "noError"
            )
            offender = _last_error_target(compatible, version)
            sent_sets = [
                frozenset(receipt.original_positions) for receipt in compatible
            ]
            selected = _choose_plan_positions(
                retriable,
                attempts,
                sent_sets,
                last_status,
                offender,
                budget.max_bindings_per_request,
            )

            if selected:
                excluded_offender = (
                    offender is not None
                    and offender not in selected
                    and offender in retriable
                )
                plan = IsolationPlan(
                    positions=tuple(selected),
                    bindings=tuple(bindings[position] for position in selected),
                    row=requests_made,
                    reason=(
                        "tooBig: reduce request size"
                        if last_status == "tooBig"
                        else (
                            f"{last_status} at error-index "
                            f"{compatible[-1].error_index}: deferred binding"
                            if excluded_offender
                            else (
                                f"{last_status}: splitting unresolved bindings"
                                if last_status != "noError"
                                else "re-query unresolved bindings"
                            )
                        )
                    ),
                )
                for position in pending:
                    if position not in selected:
                        _set_state(
                            states,
                            position,
                            BindingState.UNCONFIRMED,
                            reason="queued for a later round",
                        )
            else:  # pragma: no cover - defensive
                exhausted = True
        else:
            for position in pending:
                _set_state(
                    states,
                    position,
                    BindingState.UNCONFIRMED,
                    reason=states[position].get("reason")
                    or "retry budget exhausted",
                )
            exhausted = True

    outcomes = tuple(
        BindingOutcome(
            position=position,
            request_oid=_binding_oid(binding),
            request_binding=binding,
            state=states[position].get("state", BindingState.UNCONFIRMED),
            value=states[position].get("value"),
            error_status=states[position].get("error_status"),
            error_index=states[position].get("error_index"),
            reason=states[position].get("reason"),
            receipts=tuple(states[position].get("receipts", ())),
        )
        for position, binding in enumerate(bindings)
    )

    return IsolationResult(
        original_request=bindings,
        outcomes=outcomes,
        plan=plan,
        receipts=compatible,
        ignored_receipts=ignored,
        exhausted=exhausted,
        budget=budget,
        sampling_window=sampling_window,
        version=version,
    )


def isolation_plan_from_receipt(
    request_bindings: Sequence[Any], receipt: Receipt, **options
) -> IsolationResult:
    """Plan from one real receipt."""
    return isolation_plan(request_bindings, (receipt,), **options)


def merge_isolation_results(
    primary: IsolationResult, *additional: IsolationResult
) -> IsolationResult:
    """Merge isolation results into one coherent result.

    Confirmed or absent outcomes from ``primary`` are reused as-is.  Other
    results may fill unresolved bindings only when their sampling window is
    compatible with ``primary.sampling_window``; incompatible receipts are
    kept in the audit trail but change no binding.  A conflict always
    survives a merge.
    """
    if not additional:
        return primary

    base = primary.original_request
    for result in additional:
        if len(result.original_request) != len(base):
            raise ValueError("cannot merge results for different requests")
        for first, second in zip(base, result.original_request):
            if _binding_key(first) != _binding_key(second):
                raise ValueError("cannot merge results for different bindings")

    outcomes: List[BindingOutcome] = []
    receipts: List[Receipt] = list(primary.receipts)
    ignored: List[Receipt] = list(primary.ignored_receipts)
    exhausted = primary.exhausted

    compatible_results = [
        result
        for result in additional
        if sampling_windows_compatible(
            result.sampling_window, primary.sampling_window
        )
    ]
    ignored_results = [
        result
        for result in additional
        if not sampling_windows_compatible(
            result.sampling_window, primary.sampling_window
        )
    ]

    for position, base_binding in enumerate(base):
        current = primary.outcome_at(position)
        if current.is_conflict:
            # A conflict is evidence about the agent, not a missing value;
            # a later compatible receipt must not paper over it.
            outcomes.append(current)
            continue
        if current.is_confirmed or current.is_absent:
            outcomes.append(current)
            continue

        chosen = None
        conflict = None
        for result in compatible_results:
            outcome = result.outcome_at(position)
            if outcome.is_conflict:
                conflict = outcome
            elif outcome.is_confirmed or outcome.is_absent:
                chosen = outcome
                break

        if chosen is not None:
            outcomes.append(
                BindingOutcome(
                    position=position,
                    request_oid=chosen.request_oid,
                    request_binding=base_binding,
                    state=chosen.state,
                    value=chosen.value,
                    error_status=chosen.error_status,
                    error_index=chosen.error_index,
                    reason=chosen.reason,
                    receipts=chosen.receipts,
                )
            )
        elif conflict is not None:
            outcomes.append(
                BindingOutcome(
                    position=position,
                    request_oid=conflict.request_oid,
                    request_binding=base_binding,
                    state=BindingState.CONFLICT,
                    error_status=conflict.error_status,
                    error_index=conflict.error_index,
                    reason=conflict.reason,
                    receipts=conflict.receipts,
                )
            )
        else:
            outcomes.append(
                BindingOutcome(
                    position=position,
                    request_oid=current.request_oid,
                    request_binding=base_binding,
                    state=BindingState.UNCONFIRMED,
                    error_status=current.error_status,
                    error_index=current.error_index,
                    reason=(
                        current.reason
                        or "not settled by compatible receipts"
                    ),
                    receipts=current.receipts,
                )
            )
            exhausted = True

    for result in compatible_results:
        receipts.extend(result.receipts)
        ignored.extend(result.ignored_receipts)
    for result in ignored_results:
        ignored.extend(result.receipts)

    return IsolationResult(
        original_request=base,
        outcomes=tuple(outcomes),
        plan=None,
        receipts=tuple(receipts),
        ignored_receipts=tuple(ignored),
        exhausted=exhausted,
        budget=primary.budget,
        sampling_window=primary.sampling_window,
        version=primary.version,
    )


async def execute_isolation_plan(
    send,
    request_bindings: Sequence[Any],
    *,
    receipts: Sequence[Receipt] = (),
    version: int = SNMP_V2C,
    budget: Optional[RetryBudget] = None,
    request_size: Optional[int] = None,
    sampling_window: Optional[Any] = None,
    reused: Sequence[BindingOutcome] = (),
    on_plan=None,
) -> IsolationResult:
    """Run an isolation loop using a caller-supplied async sender.

    ``send`` is awaited with an :class:`IsolationPlan` and must return a
    :class:`Receipt` for that exact request.  Each round is planned from the
    receipt that really arrived; no binding is prefilled or assumed empty.

    ``on_plan``, when supplied, is called with each plan before it is sent,
    which makes the request sequence easy to log or audit.
    """
    collected: List[Receipt] = list(receipts)

    while True:
        result = isolation_plan(
            request_bindings,
            collected,
            version=version,
            budget=budget,
            request_size=request_size,
            sampling_window=sampling_window,
            reused=reused,
        )
        if result.plan is None:
            return result

        plan = result.plan
        if on_plan is not None:
            on_plan(plan)

        receipt = await send(plan)
        if not isinstance(receipt, Receipt):
            raise TypeError("send() must return a Receipt")

        if receipt.positions is None:
            receipt = Receipt(
                request_bindings=receipt.request_bindings,
                error_status=receipt.error_status,
                error_index=receipt.error_index,
                values=receipt.values,
                error_indication=receipt.error_indication,
                version=receipt.version,
                positions=plan.positions,
                request_id=receipt.request_id,
                response_request_id=receipt.response_request_id,
                matched=receipt.matched,
                sampling_window=receipt.sampling_window,
                row=receipt.row or plan.row,
            )
        collected.append(receipt)


class IsolationPlanner:
    """Stateful driver for repeated rounds of error isolation.

    The planner never sends anything by itself.  Callers obtain the next
    :class:`IsolationPlan`, perform the GET, and submit the real receipt with
    :meth:`record`.  This keeps the request sequence auditable and lets the
    caller reuse bindings it has already confirmed.

    Examples
    --------
    >>> planner = IsolationPlanner(original_bindings)
    >>> while (plan := planner.next_plan()) is not None:
    ...     receipt = await send_get(plan.bindings)
    ...     planner.record(receipt, positions=plan.positions)
    >>> result = planner.result()
    """

    def __init__(
        self,
        request_bindings: Sequence[Any],
        *,
        version: int = SNMP_V2C,
        budget: Optional[RetryBudget] = None,
        request_size: Optional[int] = None,
        sampling_window: Optional[Any] = None,
        reused: Sequence[BindingOutcome] = (),
    ):
        """Create a planner for an original request."""
        self.original_request = tuple(request_bindings)
        self.version = version
        self.budget = (budget or RetryBudget()).with_request_size(request_size)
        self.sampling_window = sampling_window
        self.reused = tuple(reused)
        self._receipts: List[Receipt] = []
        self._round = 0

    @property
    def receipts(self) -> Tuple[Receipt, ...]:
        """Receipts recorded so far."""
        return tuple(self._receipts)

    def next_plan(self) -> Optional[IsolationPlan]:
        """Return the next plan to execute, or ``None`` when done."""
        return isolation_plan(
            self.original_request,
            self._receipts,
            version=self.version,
            budget=self.budget,
            sampling_window=self.sampling_window,
            reused=self.reused,
        ).plan

    def record(
        self,
        receipt: Receipt,
        positions: Optional[Sequence[int]] = None,
        *,
        row: Optional[int] = None,
    ) -> IsolationResult:
        """Record a real receipt and return the updated result.

        ``positions`` must be the plan positions if the receipt does not
        already carry them; without either, the receipt cannot be mapped
        back to the original request and :class:`ValueError` is raised.
        The recorded row defaults to the current follow-up round so receipts
        stay ordered in the audit trail.
        """
        if receipt.positions is None:
            if positions is None:
                raise ValueError(
                    "receipt needs positions to map back to the original request"
                )
            receipt = Receipt(
                request_bindings=receipt.request_bindings,
                error_status=receipt.error_status,
                error_index=receipt.error_index,
                values=receipt.values,
                error_indication=receipt.error_indication,
                version=receipt.version,
                positions=positions,
                request_id=receipt.request_id,
                response_request_id=receipt.response_request_id,
                matched=receipt.matched,
                sampling_window=receipt.sampling_window,
                row=self._round if row is None else row,
            )
        elif row is not None:
            receipt = Receipt(
                request_bindings=receipt.request_bindings,
                error_status=receipt.error_status,
                error_index=receipt.error_index,
                values=receipt.values,
                error_indication=receipt.error_indication,
                version=receipt.version,
                positions=receipt.positions,
                request_id=receipt.request_id,
                response_request_id=receipt.response_request_id,
                matched=receipt.matched,
                sampling_window=receipt.sampling_window,
                row=row,
            )
        self._receipts.append(receipt)
        self._round += 1
        return self.result()

    def result(self) -> IsolationResult:
        """Return the current result without planning."""
        return isolation_plan(
            self.original_request,
            self._receipts,
            version=self.version,
            budget=self.budget,
            sampling_window=self.sampling_window,
            reused=self.reused,
        )
