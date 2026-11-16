"""Compaction of ``org.freedesktop.DBus.Properties.PropertiesChanged`` signals.

A consumer that ingests property updates usually does not need to keep every
signal as its own record. Several consecutive signals for the same interface
can be folded into one batch as long as doing so cannot change the answer to
any observation point the consumer must answer.

The compaction is therefore answer-preserving by construction: the input is
first replayed one signal at a time to get a reference answer for every
observation point, then adjacent signals are merged greedily. A merge is kept
only when every observation point gets exactly the same status, value, and
reason from the merged batches as it got from the one-signal-at-a-time
reference. The resulting records explain every kept merge and every split.

The rules implemented here follow the ``EmitsChangedSignal`` property
annotation:

``true`` (the D-Bus default)
    The signal carries the new value. The reported value may be used.
``invalidated``
    The signal only says that the cached value is no longer valid. The value
    must be re-read; the previous value is never reused. If such a signal
    carries a value in the ``changed`` dictionary anyway, the value is dropped
    and the property is treated as invalidated.
``const``
    The property never changes. A signal that reports a change is a
    contradiction, so the value becomes unknown instead of being trusted.
``false``
    The property may change without a signal. A delivered value still cannot
    be used as a cacheable value, and "did it change?" cannot be answered from
    signals alone.

A gap in a ``(path, interface)`` stream means an unknown signal may have
changed any property of that interface. Those properties stay unknown until a
later signal delivers a value for them. Properties of other interfaces are not
affected.

Signals on interfaces listed in :attr:`CompactionConfig.event_interfaces` are
events rather than state updates. Every signal is kept as its own batch so no
occurrence is lost.

The module can also be used as a command line tool::

    python -m dbus_next.property_compaction input.json
"""

from .constants import EmitsChangedSignal, MessageType

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import copy
import json
import sys

PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
PROPERTIES_CHANGED = 'PropertiesChanged'

KNOWN = 'known'
UNKNOWN = 'unknown'

REASON_NEVER_REPORTED = 'never-reported'
REASON_INVALIDATED = 'invalidated'
REASON_STREAM_GAP = 'stream-gap'
REASON_NOT_CACHEABLE = 'not-cacheable'
REASON_CONST_CHANGED = 'const-property-changed'
REASON_UNREPORTED_CHANGES = 'unreported-changes'

SPLIT_MERGING_DISABLED = 'merging-disabled'
SPLIT_EVENT_INTERFACE = 'event-interface'
SPLIT_STREAM_SWITCH = 'stream-switch'
SPLIT_STREAM_GAP = 'stream-gap'
SPLIT_OBSERVATION = 'observation-boundary'


class ObservationKind:
    """The question an observation point asks about a property.

    ``value``
        The value the consumer may cache at the observation boundary.
    ``changed``
        Whether a change for the property was reported in the window
        ``(after_serial, at_serial]``.
    """
    VALUE = 'value'
    CHANGED = 'changed'

    _ALL = (VALUE, CHANGED)

    @staticmethod
    def parse(value) -> str:
        if value not in ObservationKind._ALL:
            raise ValueError(f'invalid observation kind: {value!r}')
        return value


class PropertyChange:
    """One numbered ``PropertiesChanged`` signal.

    :ivar serial: The position of this signal in the input. Signals must be
        given in strictly increasing serial order.
    :vartype serial: int
    :ivar interface: The interface the properties belong to.
    :vartype interface: str
    :ivar changed: The properties reported with a new value.
    :vartype changed: dict(str, Any)
    :ivar invalidated: The properties reported as invalid.
    :vartype invalidated: list(str)
    :ivar path: The object path the signal was emitted from.
    :vartype path: str
    :ivar sequence: An optional per-stream sequence number. Signals of the
        same ``(path, interface)`` stream must increase by one unless the
        missing numbers are a gap. When omitted, a gap is inferred from the
        serials only when the skipped serials do not appear anywhere in the
        input, so an interleaved signal of another interface is not mistaken
        for a gap.
    :vartype sequence: int

    A property listed in both ``changed`` and ``invalidated`` is accepted; the
    invalidation wins because the value cannot be trusted.
    """
    def __init__(self,
                 serial: int,
                 interface: str,
                 changed: Optional[Mapping[str, Any]] = None,
                 invalidated: Optional[Iterable[str]] = None,
                 path: str = '/',
                 sequence: Optional[int] = None):
        if type(serial) is not int or serial < 0:
            raise ValueError('serial must be a non-negative integer')
        if not isinstance(interface, str) or not interface:
            raise ValueError('interface must be a non-empty string')
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ValueError('sequence must be a non-negative integer')

        self.serial = serial
        self.sequence = sequence
        self.interface = interface
        self.path = path
        self.changed = dict(changed) if changed else {}
        self.invalidated = list(invalidated) if invalidated else []

    @property
    def effective_sequence(self) -> int:
        return self.serial if self.sequence is None else self.sequence

    @staticmethod
    def from_message(message, sequence: Optional[int] = None) -> 'PropertyChange':
        """Build a change from a low-level ``PropertiesChanged`` signal.

        Values carried as :class:`Variant <dbus_next.Variant>` instances are
        unwrapped so that the compaction result holds plain values.

        :param message: A :class:`Message <dbus_next.Message>` received from
            the bus.
        :param sequence: Optional per-stream sequence number. Defaults to the
            message serial.
        :raises:
            - :class:`ValueError` - If the message is not a
              ``PropertiesChanged`` signal.
        """
        if message.message_type != MessageType.SIGNAL:
            raise ValueError('the message is not a signal')
        if message.interface != PROPERTIES_INTERFACE:
            raise ValueError(f'the message is not on the {PROPERTIES_INTERFACE} interface')
        if message.member != PROPERTIES_CHANGED:
            raise ValueError(f'the message is not a {PROPERTIES_CHANGED} signal')
        if message.signature != 'sa{sv}as':
            raise ValueError(f'the message has an unexpected signature: "{message.signature}"')

        interface_name, changed_variants, invalidated = message.body
        changed = {}
        for name, variant in changed_variants.items():
            changed[name] = copy.deepcopy(getattr(variant, 'value', variant))
        return PropertyChange(message.serial,
                              interface_name,
                              changed,
                              list(invalidated),
                              path=message.path,
                              sequence=sequence)

    def __repr__(self):
        return (f'<PropertyChange serial={self.serial} interface={self.interface!r} '
                f'changed={list(self.changed)} invalidated={self.invalidated}>')


class ObservationPoint:
    """A question that must be answered from the compacted output.

    :ivar id: A unique identifier for the observation.
    :vartype id: str
    :ivar interface: The interface of the property.
    :vartype interface: str
    :ivar property_name: The name of the property.
    :vartype property_name: str
    :ivar at_serial: The signal serial the observation is evaluated at,
        inclusive. ``None`` means after the last input signal.
    :vartype at_serial: int
    :ivar kind: One of the :class:`ObservationKind` values.
    :vartype kind: str
    :ivar path: The object path of the property.
    :vartype path: str
    :ivar after_serial: For ``changed`` observations, the start of the
        window, exclusive. Defaults to 0.
    :vartype after_serial: int
    """
    def __init__(self,
                 id: str,
                 interface: str,
                 property_name: str,
                 at_serial: Optional[int] = None,
                 kind: str = ObservationKind.VALUE,
                 path: str = '/',
                 after_serial: int = 0):
        if not isinstance(id, str) or not id:
            raise ValueError('id must be a non-empty string')
        if not isinstance(interface, str) or not interface:
            raise ValueError('interface must be a non-empty string')
        if not isinstance(property_name, str) or not property_name:
            raise ValueError('property_name must be a non-empty string')
        if at_serial is not None and (type(at_serial) is not int or at_serial < 0):
            raise ValueError('at_serial must be a non-negative integer or None')
        if type(after_serial) is not int or after_serial < 0:
            raise ValueError('after_serial must be a non-negative integer')

        self.id = id
        self.interface = interface
        self.property_name = property_name
        self.at_serial = at_serial
        self.kind = ObservationKind.parse(kind)
        self.path = path
        self.after_serial = after_serial

    def resolved_serial(self, horizon: int) -> int:
        return horizon if self.at_serial is None else self.at_serial

    def __repr__(self):
        return (f'<ObservationPoint {self.id} {self.interface}.{self.property_name} '
                f'kind={self.kind} at={self.at_serial}>')


class CompactionConfig:
    """Configuration for :func:`compact_property_changes`.

    :ivar event_interfaces: Interfaces whose signals describe events. Every
        signal on these interfaces is kept as its own batch.
    :vartype event_interfaces: set(str)
    :ivar annotations: A mapping from ``(interface, property)``,
        ``"interface:property"``, or a property name to an
        :class:`EmitsChangedSignal <dbus_next.EmitsChangedSignal>`. A bare
        property name applies to all interfaces. A nested mapping
        ``{interface: {property: annotation}}`` is accepted as well, which is
        convenient for JSON input. Properties without an annotation default to
        ``true``, which is the D-Bus default.
    :vartype annotations: dict
    :ivar detect_gaps: Whether gaps in a ``(path, interface)`` stream are
        detected. Defaults to ``True``.
    :vartype detect_gaps: bool
    :ivar dense_serials: Whether a gap may be inferred from a serial jump when
        a signal has no explicit ``sequence``. The inference only treats a
        skipped serial as missing when it does not appear anywhere in the
        input, so a signal of another interface interleaved between two
        signals is not mistaken for a gap. Keep this off when the serials come
        from a real bus connection that also carries unrelated traffic.
        Defaults to ``False``.
    :vartype dense_serials: bool
    :ivar merge: Whether adjacent signals may be folded at all. Defaults to
        ``True``.
    :vartype merge: bool
    """
    def __init__(self,
                 event_interfaces: Optional[Iterable[str]] = None,
                 annotations: Optional[Mapping] = None,
                 detect_gaps: bool = True,
                 merge: bool = True,
                 dense_serials: bool = False):
        self.event_interfaces = frozenset(event_interfaces or ())
        self.annotations = {}
        self.detect_gaps = bool(detect_gaps)
        self.merge = bool(merge)
        self.dense_serials = bool(dense_serials)

        for key, value in (annotations or {}).items():
            if type(key) is tuple:
                if len(key) != 2 or not all(isinstance(part, str) and part for part in key):
                    raise ValueError(
                        'annotation keys must be (interface, property) tuples or property names')
                self.annotations[key] = EmitsChangedSignal.parse(value)
            elif isinstance(key, str) and key:
                if isinstance(value, Mapping):
                    for property_name, annotation in value.items():
                        if not isinstance(property_name, str) or not property_name:
                            raise ValueError('annotation property names must be non-empty strings')
                        self.annotations[(key, property_name)] = EmitsChangedSignal.parse(
                            annotation)
                elif ':' in key and key.count(':') == 1:
                    interface, property_name = key.split(':')
                    if not interface or not property_name:
                        raise ValueError(
                            'annotation keys must be "interface:property" or property names')
                    self.annotations[(interface, property_name)] = EmitsChangedSignal.parse(
                        value)
                else:
                    self.annotations[key] = EmitsChangedSignal.parse(value)
            else:
                raise ValueError(
                    'annotation keys must be (interface, property) tuples or property names')

    def annotation_for(self, interface: str, property_name: str) -> EmitsChangedSignal:
        """Get the annotation for a property, falling back to the D-Bus
        default of ``true``."""
        value = self.annotations.get((interface, property_name))
        if value is None:
            value = self.annotations.get(property_name)
        return EmitsChangedSignal.TRUE if value is None else value

    @staticmethod
    def from_introspection(*interfaces,
                           event_interfaces: Optional[Iterable[str]] = None,
                           detect_gaps: bool = True,
                           merge: bool = True,
                           dense_serials: bool = False) -> 'CompactionConfig':
        """Build a configuration from introspected interfaces.

        :param interfaces: :class:`Interface
            <dbus_next.introspection.Interface>` instances whose property
            annotations are used.
        :param event_interfaces: Interfaces whose signals are events.
        :param detect_gaps: Whether to detect stream gaps.
        :param merge: Whether adjacent signals may be folded.
        :param dense_serials: Whether serials alone may indicate a gap.
        """
        return CompactionConfig(event_interfaces=event_interfaces,
                                annotations=annotation_map(*interfaces),
                                detect_gaps=detect_gaps,
                                merge=merge,
                                dense_serials=dense_serials)


class PropertyBatch:
    """A compacted ingestion batch.

    A batch is the unit a consumer ingests. It holds the state at the end of
    the folded signals, not the intermediate states.

    :ivar index: The position of this batch in the result.
    :vartype index: int
    :ivar interface: The interface of the batch.
    :vartype interface: str
    :ivar path: The object path of the batch.
    :vartype path: str
    :ivar signals: The source signals folded into this batch, in order.
    :vartype signals: list(PropertyChange)
    :ivar changed: The last value reported for each property in the batch, in
        the order the properties were first reported.
    :vartype changed: dict(str, Any)
    :ivar invalidated: The properties whose value is invalid at the end of the
        batch, in the order they became invalid.
    :vartype invalidated: list(str)
    :ivar event: Whether this batch is a single event-interface signal.
    :vartype event: bool
    :ivar gap_before: Whether an undelivered signal may have changed properties
        of this interface before this batch. Properties of this interface that
        are not listed in ``changed`` must then be treated as unknown.
    :vartype gap_before: bool
    :ivar split_reason: Why this batch is not folded into the previous batch,
        or ``None`` for the first batch.
    :vartype split_reason: str
    :ivar notes: Audit notes about values dropped or resolved while building
        the batch.
    :vartype notes: list(dict)
    """
    def __init__(self,
                 index: int,
                 interface: str,
                 path: str,
                 event: bool = False,
                 split_reason: Optional[str] = None):
        self.index = index
        self.interface = interface
        self.path = path
        self.event = event
        self.split_reason = split_reason
        self.gap_before = False
        self.signals = []
        self.changed = {}
        self.invalidated = {}
        self.invalidated_reasons = {}
        self.notes = []

    @property
    def merged(self) -> bool:
        """Whether more than one signal was folded into this batch."""
        return len(self.signals) > 1

    @property
    def start_serial(self) -> int:
        return self.signals[0].serial if self.signals else 0

    @property
    def end_serial(self) -> int:
        return self.signals[-1].serial if self.signals else 0

    @property
    def serials(self) -> List[int]:
        return [signal.serial for signal in self.signals]

    @property
    def invalidated_properties(self) -> List[str]:
        return list(self.invalidated)

    def apply_value(self, property_name: str, value: Any):
        self.changed[property_name] = value
        self.invalidated.pop(property_name, None)
        self.invalidated_reasons.pop(property_name, None)

    def apply_invalidation(self, property_name: str, reason: str = REASON_INVALIDATED):
        self.changed.pop(property_name, None)
        self.invalidated[property_name] = True
        self.invalidated_reasons[property_name] = reason

    def absorb(self, other: 'PropertyBatch'):
        """Fold another batch of the same stream into this one."""
        self.signals.extend(other.signals)
        self.notes.extend(other.notes)
        self.gap_before = self.gap_before or other.gap_before
        for property_name, value in other.changed.items():
            self.apply_value(property_name, value)
        for property_name in other.invalidated:
            self.apply_invalidation(
                property_name, other.invalidated_reasons.get(property_name,
                                                             REASON_INVALIDATED))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'index': self.index,
            'interface': self.interface,
            'path': self.path,
            'start_serial': self.start_serial,
            'end_serial': self.end_serial,
            'serials': self.serials,
            'signals': [source_to_dict(signal) for signal in self.signals],
            'changed': {name: json_value(value) for name, value in self.changed.items()},
            'invalidated': self.invalidated_properties,
            'event': self.event,
            'gap_before': self.gap_before,
            'merged': self.merged,
            'split_reason': self.split_reason,
            'notes': self.notes,
        }

    def __repr__(self):
        return (f'<PropertyBatch index={self.index} interface={self.interface!r} '
                f'serials={self.serials} merged={self.merged} event={self.event}>')


class StreamGap:
    """A detected gap in the sequence of a ``(path, interface)`` stream.

    :ivar path: The object path of the stream.
    :vartype path: str
    :ivar interface: The interface of the stream.
    :vartype interface: str
    :ivar after_sequence: The last sequence number that was received before
        the gap.
    :vartype after_sequence: int
    :ivar before_sequence: The first sequence number that was received after
        the gap.
    :vartype before_sequence: int
    :ivar after_serial: The serial of the last signal received before the gap.
    :vartype after_serial: int
    """
    def __init__(self, path: str, interface: str, after_sequence: int, before_sequence: int,
                 after_serial: int):
        self.path = path
        self.interface = interface
        self.after_sequence = after_sequence
        self.before_sequence = before_sequence
        self.after_serial = after_serial

    @property
    def missing_count(self) -> int:
        return self.before_sequence - self.after_sequence - 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            'path': self.path,
            'interface': self.interface,
            'after_sequence': self.after_sequence,
            'before_sequence': self.before_sequence,
            'after_serial': self.after_serial,
            'missing_count': self.missing_count,
        }

    def __repr__(self):
        return (f'<StreamGap interface={self.interface!r} '
                f'after={self.after_sequence} before={self.before_sequence}>')


class ObservationState:
    """The knowable state of one observation point.

    :ivar id: The observation id.
    :vartype id: str
    :ivar status: ``known`` or ``unknown``.
    :vartype status: str
    :ivar value: The answer when the status is known, otherwise ``None``.
    :vartype value: Any
    :ivar reason: Why the answer is unknown, otherwise ``None``.
    :vartype reason: str
    :ivar interface: The interface of the observed property.
    :vartype interface: str
    :ivar path: The object path of the observed property.
    :vartype path: str
    :ivar property_name: The observed property.
    :vartype property_name: str
    :ivar at_serial: The serial the observation was evaluated at.
    :vartype at_serial: int
    :ivar kind: The observation kind.
    :vartype kind: str
    :ivar annotation: The effective EmitsChangedSignal annotation.
    :vartype annotation: str
    :ivar batch: The compacted batch that covers the observation, or ``None``.
    :vartype batch: int
    :ivar serial: The serial that established the answer, or ``None``.
    :vartype serial: int
    """
    def __init__(self,
                 observation: ObservationPoint,
                 status: str,
                 at_serial: int,
                 value: Any = None,
                 reason: Optional[str] = None,
                 annotation: Optional[EmitsChangedSignal] = None,
                 batch: Optional[int] = None,
                 serial: Optional[int] = None):
        self.id = observation.id
        self.status = status
        self.value = value
        self.reason = reason
        self.interface = observation.interface
        self.path = observation.path
        self.property_name = observation.property_name
        self.at_serial = at_serial
        self.kind = observation.kind
        self.annotation = (annotation or EmitsChangedSignal.TRUE).value
        self.batch = batch
        self.serial = serial

    @property
    def known(self) -> bool:
        return self.status == KNOWN

    def answer(self) -> Tuple[str, Any, Optional[str]]:
        """The part of the state an observation actually asks for."""
        return (self.status, self.value, self.reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'interface': self.interface,
            'path': self.path,
            'property': self.property_name,
            'at_serial': self.at_serial,
            'kind': self.kind,
            'annotation': self.annotation,
            'status': self.status,
            'value': json_value(self.value),
            'reason': self.reason,
            'batch': self.batch,
            'serial': self.serial,
        }

    def __repr__(self):
        if self.known:
            return (f'<ObservationState {self.id} known value={self.value!r} '
                    f'annotation={self.annotation}>')
        return (f'<ObservationState {self.id} unknown reason={self.reason!r} '
                f'annotation={self.annotation}>')


class CompactionResult:
    """The compacted batches, audit record, gaps, and observation states."""
    def __init__(self):
        self.batches: List[PropertyBatch] = []
        self.gaps: List[StreamGap] = []
        self.records: List[Dict[str, Any]] = []
        self.observations: List[ObservationState] = []
        self.source_signal_count = 0

    def observation(self, id: str) -> ObservationState:
        """Get the state for an observation id.

        :raises: :class:`KeyError` if there is no observation with this id.
        """
        for state in self.observations:
            if state.id == id:
                return state
        raise KeyError(f'no observation with id "{id}"')

    def to_dict(self) -> Dict[str, Any]:
        """Get the result as JSON-friendly dictionaries."""
        return {
            'source_signals': self.source_signal_count,
            'batch_count': len(self.batches),
            'batches': [batch.to_dict() for batch in self.batches],
            'records': self.records,
            'gaps': [gap.to_dict() for gap in self.gaps],
            'observations': [state.to_dict() for state in self.observations],
        }

    def __repr__(self):
        return (f'<CompactionResult batches={len(self.batches)} '
                f'observations={len(self.observations)} gaps={len(self.gaps)}>')


def source_to_dict(signal: PropertyChange) -> Dict[str, Any]:
    return {
        'serial': signal.serial,
        'sequence': signal.sequence,
        'interface': signal.interface,
        'path': signal.path,
        'changed': {name: json_value(value) for name, value in signal.changed.items()},
        'invalidated': list(signal.invalidated),
    }


def json_value(value):
    """Convert a value to something ``json`` can serialize.

    D-Bus values may be bytes or file descriptors, which JSON cannot carry.
    Those are rendered as text so the audit record stays serializable.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return repr(value)


def annotation_map(*interfaces) -> Dict[Tuple[str, str], EmitsChangedSignal]:
    """Build an annotation mapping from introspected interfaces.

    Example::

        annotations = annotation_map(proxy_interface.introspection)
        config = CompactionConfig(annotations=annotations)

    :param interfaces: :class:`Interface <dbus_next.introspection.Interface>`
        instances.
    """
    result = {}
    for interface in interfaces:
        for prop in interface.properties:
            result[(interface.name, prop.name)] = getattr(prop, 'emits_changed_signal',
                                                          EmitsChangedSignal.TRUE)
    return result


class _StreamState:
    """Accumulated knowable state for one ``(path, interface, property)``."""
    def __init__(self):
        self.kind = UNKNOWN
        self.value = None
        self.serial = None
        self.reason = REASON_NEVER_REPORTED
        self.batch = None


def _normalize_operations(signal: PropertyChange, config: CompactionConfig,
                          notes: List[Dict[str, Any]]) -> List[Tuple[str, str, Any, int]]:
    '''Turn one signal into ordered (property, kind, value, serial) operations
    after applying the EmitsChangedSignal annotations.'''
    operations = []

    names = list(signal.changed)
    names.extend(name for name in signal.invalidated if name not in signal.changed)

    for name in names:
        annotation = config.annotation_for(signal.interface, name)
        in_changed = name in signal.changed
        in_invalidated = name in signal.invalidated

        if annotation == EmitsChangedSignal.CONST:
            notes.append({
                'action': 'drop',
                'serial': signal.serial,
                'property': name,
                'annotation': annotation.value,
                'reason': REASON_CONST_CHANGED,
            })
            operations.append((name, 'const', None, signal.serial))
            continue

        if annotation == EmitsChangedSignal.INVALIDATED:
            if in_changed:
                notes.append({
                    'action': 'drop',
                    'serial': signal.serial,
                    'property': name,
                    'annotation': annotation.value,
                    'reason': REASON_INVALIDATED,
                })
            operations.append((name, 'invalidate', None, signal.serial))
            continue

        if in_changed and in_invalidated:
            notes.append({
                'action': 'resolve-conflict',
                'serial': signal.serial,
                'property': name,
                'annotation': annotation.value,
                'reason': 'changed-and-invalidated',
                'resolution': 'invalidate-value',
            })
            operations.append((name, 'invalidate', None, signal.serial))
        elif in_invalidated:
            operations.append((name, 'invalidate', None, signal.serial))
        else:
            operations.append((name, 'value', signal.changed[name], signal.serial))

    return operations


def _apply_operations(batch: PropertyBatch, operations):
    for property_name, kind, value, _ in operations:
        if kind == 'value':
            batch.apply_value(property_name, value)
        elif kind == 'invalidate':
            batch.apply_invalidation(property_name)
        else:
            # A const property that changed cannot be trusted. Marking it
            # invalid keeps the value unknown instead of guessing.
            batch.apply_invalidation(property_name, REASON_CONST_CHANGED)


def _evaluate_states(observations: Sequence[ObservationPoint],
                     batch_lists: Dict[Tuple[str, str], Sequence[PropertyBatch]],
                     gap_lists: Dict[Tuple[str, str], Sequence[Tuple[int, int]]],
                     config: CompactionConfig,
                     horizon: int) -> List[ObservationState]:
    """Answer every observation from a batch list.

    A batch is atomic: an observation that falls inside a merged batch sees
    the state at the end of that batch, because the batch does not carry the
    intermediate signal boundaries.
    """
    results = []
    for observation in observations:
        at_serial = observation.resolved_serial(horizon)
        key = (observation.path, observation.interface)
        batches = batch_lists.get(key, ())
        gaps = gap_lists.get(key, ())
        results.append(
            _one_state(observation, at_serial, batches, gaps, config))
    return results


def _one_state(observation: ObservationPoint, at_serial: int,
               batches: Sequence[PropertyBatch], gaps: Sequence[Tuple[int, int]],
               config: CompactionConfig) -> ObservationState:
    property_name = observation.property_name
    annotation = config.annotation_for(observation.interface, property_name)

    def state(status, value=None, reason=None, serial=None, batch=None):
        return ObservationState(observation,
                                status,
                                at_serial,
                                value=value,
                                reason=reason,
                                annotation=annotation,
                                batch=batch,
                                serial=serial)

    if observation.kind == ObservationKind.CHANGED:
        mentions = []
        in_window_gap = False
        for gap_serial, _ in gaps:
            # A gap begins after the signal at gap_serial, so it belongs to
            # the window when the window still extends past that signal.
            if gap_serial >= observation.after_serial and gap_serial < at_serial:
                in_window_gap = True
                break
        for batch in batches:
            if batch.start_serial > at_serial:
                break
            if batch.end_serial <= observation.after_serial:
                continue
            if property_name in batch.changed or property_name in batch.invalidated:
                mentions.append(batch)
        if mentions:
            # Any batch that still mentions the property proves that a change
            # for it was reported in the window, even when the annotation says
            # the property cannot change.
            return state(KNOWN, True, serial=mentions[0].end_serial,
                         batch=mentions[0].index)
        if in_window_gap:
            return state(UNKNOWN, reason=REASON_STREAM_GAP)
        if annotation == EmitsChangedSignal.FALSE:
            return state(UNKNOWN, reason=REASON_UNREPORTED_CHANGES)
        return state(KNOWN, False)

    if annotation == EmitsChangedSignal.FALSE:
        return state(UNKNOWN, reason=REASON_NOT_CACHEABLE)

    stream_state = _replay_value(observation, at_serial, batches, gaps)
    return state(stream_state.kind,
                 value=stream_state.value,
                 reason=None if stream_state.kind == KNOWN else stream_state.reason,
                 serial=stream_state.serial,
                 batch=stream_state.batch)


def _replay_value(observation: ObservationPoint, at_serial: int,
                  batches: Sequence[PropertyBatch],
                  gaps: Sequence[Tuple[int, int]]) -> _StreamState:
    """Replay a stream as a consumer would, treating each batch atomically."""
    property_name = observation.property_name
    state = _StreamState()

    items = []
    for gap_serial, gap_index in gaps:
        items.append(((gap_serial, 1), 'gap', gap_serial, None))
    for batch in batches:
        items.append(((batch.start_serial, 0), 'batch', batch.start_serial, batch))
    items.sort(key=lambda item: item[0])

    for _, kind, position, payload in items:
        if kind == 'batch':
            if position > at_serial:
                break
            batch = payload
            if property_name in batch.changed:
                state.kind = KNOWN
                state.value = batch.changed[property_name]
                state.serial = batch.end_serial
                state.reason = None
                state.batch = batch.index
            elif property_name in batch.invalidated:
                state.kind = UNKNOWN
                state.value = None
                state.serial = batch.end_serial
                state.reason = batch.invalidated_reasons.get(property_name,
                                                             REASON_INVALIDATED)
                state.batch = batch.index
        else:
            if position >= at_serial:
                # The gap is after the signal the observation is evaluated at,
                # so it cannot affect the answer.
                break
            # A gap makes every property of the interface unknown at once.
            state.kind = UNKNOWN
            state.value = None
            state.serial = position
            state.reason = REASON_STREAM_GAP
            state.batch = _batch_index_before(batches, position)

    if state.kind == KNOWN:
        return state
    if state.reason is None:
        state.reason = REASON_NEVER_REPORTED
    return state


def _batch_index_before(batches: Sequence[PropertyBatch], serial: int):
    index = None
    for batch in batches:
        if batch.end_serial <= serial:
            index = batch.index
    return index


def _answers(states: Sequence[ObservationState]):
    return [state.answer() for state in states]


def _structural_split(a: PropertyBatch, b: PropertyBatch, gap_after_a: bool,
                      config: CompactionConfig) -> Optional[str]:
    if not config.merge:
        return SPLIT_MERGING_DISABLED
    if a.event or b.event:
        return SPLIT_EVENT_INTERFACE
    if (a.path, a.interface) != (b.path, b.interface):
        return SPLIT_STREAM_SWITCH
    if gap_after_a:
        return SPLIT_STREAM_GAP
    return None


def compact_property_changes(
        signals: Iterable[PropertyChange],
        observations: Optional[Iterable[ObservationPoint]] = None,
        config: Optional[CompactionConfig] = None) -> CompactionResult:
    """Compact numbered property changes into answer-preserving batches and
    answer the observation points.

    Signals must be given in increasing serial order. The input is replayed one
    signal at a time to establish the reference answer for every observation
    point. Adjacent signals of the same ``(path, interface)`` stream are then
    merged greedily, and a merge is kept only when every observation point
    keeps exactly the same status, value, and reason.

    Signals on event interfaces are always one batch per signal. A gap does
    not create a synthetic value: properties of the affected interface stay
    unknown until a later signal reports a value for them. Properties of other
    interfaces are unaffected.

    :param signals: The numbered :class:`PropertyChange` signals.
    :param observations: The :class:`ObservationPoint` instances that must be
        answered.
    :param config: The :class:`CompactionConfig`. Defaults to a configuration
        with no event interfaces and no annotations.
    :raises:
        - :class:`ValueError` - If the signals are out of order or a stream
          sequence goes backwards.
    """
    config = config or CompactionConfig()
    signals = [parse_property_change(item) for item in signals]
    observations = [parse_observation(item) for item in (observations or ())]

    horizon = signals[-1].serial if signals else 0
    for observation in observations:
        resolved = observation.resolved_serial(horizon)
        if resolved < observation.after_serial:
            raise ValueError(
                f'observation "{observation.id}" ends before its window starts')

    # 1. Validate ordering and build one atomic batch per signal, recording
    #    gaps between signals of the same stream.
    atomic_batches: List[PropertyBatch] = []
    gaps: List[StreamGap] = []
    gaps_by_key: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
    last_serial = None
    last_sequence: Dict[Tuple[str, str], int] = {}
    last_serial_in_stream: Dict[Tuple[str, str], int] = {}
    sequence_mode: Dict[Tuple[str, str], bool] = {}
    seen_serials = {signal.serial for signal in signals}

    for signal in signals:
        if last_serial is not None and signal.serial <= last_serial:
            raise ValueError(
                f'signals must have strictly increasing serials '
                f'(got {signal.serial} after {last_serial})')
        last_serial = signal.serial

        key = (signal.path, signal.interface)
        explicit_sequence = signal.sequence is not None
        if key in sequence_mode and sequence_mode[key] != explicit_sequence:
            raise ValueError(
                f'signals of interface "{signal.interface}" must consistently '
                f'provide or omit a sequence number')
        sequence_mode[key] = explicit_sequence
        sequence = signal.effective_sequence

        has_gap = False
        if config.detect_gaps and key in last_sequence:
            previous_sequence = last_sequence[key]
            if sequence <= previous_sequence:
                raise ValueError(
                    f'signal sequence must increase within a stream '
                    f'(got {sequence} after {previous_sequence} '
                    f'for interface "{signal.interface}")')
            if explicit_sequence:
                has_gap = sequence > previous_sequence + 1
            elif config.dense_serials:
                has_gap = any(serial not in seen_serials for serial in
                              range(last_serial_in_stream[key] + 1, signal.serial))

        if has_gap:
            gap = StreamGap(signal.path, signal.interface, last_sequence[key], sequence,
                            last_serial_in_stream[key])
            gaps.append(gap)
            gaps_by_key.setdefault(key, []).append((gap.after_serial,
                                                    len(gaps_by_key.get(key, ()))))

        last_sequence[key] = sequence
        last_serial_in_stream[key] = signal.serial

        batch = PropertyBatch(len(atomic_batches),
                              signal.interface,
                              signal.path,
                              event=signal.interface in config.event_interfaces)
        batch.gap_before = has_gap
        batch.signals.append(signal)
        _apply_operations(batch, _normalize_operations(signal, config, batch.notes))
        atomic_batches.append(batch)

    def batch_lists(batches: Sequence[PropertyBatch]):
        result: Dict[Tuple[str, str], List[PropertyBatch]] = {}
        for batch in batches:
            result.setdefault((batch.path, batch.interface), []).append(batch)
        return result

    reference = _answers(
        _evaluate_states(observations, batch_lists(atomic_batches), gaps_by_key, config,
                         horizon))

    # 2. Greedily merge adjacent batches when the structural rules allow it and
    #    the observation answers do not move.
    def gap_between(a: PropertyBatch, b: PropertyBatch) -> bool:
        for gap in gaps:
            if gap.path != a.path or gap.interface != a.interface:
                continue
            if a.end_serial <= gap.after_serial < b.start_serial:
                return True
        return False

    batches = list(atomic_batches)
    i = 0
    while i < len(batches) - 1:
        a = batches[i]
        b = batches[i + 1]
        reason = _structural_split(a, b, gap_between(a, b), config)
        if reason is not None:
            b.split_reason = reason
            i += 1
            continue

        merged = _clone_batch(a, a.index)
        merged.absorb(b)
        tentative = batches[:i] + [merged] + batches[i + 2:]
        tentative_answers = _answers(
            _evaluate_states(observations, batch_lists(tentative), gaps_by_key, config,
                             horizon))
        if tentative_answers == reference:
            merged.split_reason = a.split_reason
            batches = tentative
            # Stay at the same position so the merged batch can absorb the
            # next one as well.
            continue

        b.split_reason = SPLIT_OBSERVATION
        i += 1

    # 3. Renumber and build the audit record and the final answers.
    def gap_before(batch: PropertyBatch) -> bool:
        for gap in gaps:
            if gap.path != batch.path or gap.interface != batch.interface:
                continue
            if gap.after_serial < batch.start_serial:
                return True
        return False

    for index, batch in enumerate(batches):
        batch.index = index
        batch.gap_before = gap_before(batch)

    for batch in batches:
        if batch.event and len(batch.signals) != 1:
            raise AssertionError('event batches must hold exactly one signal')

    states = _evaluate_states(observations, batch_lists(batches), gaps_by_key, config,
                              horizon)

    result = CompactionResult()
    result.batches = batches
    result.gaps = gaps
    result.observations = states
    result.source_signal_count = len(signals)
    result.records = _build_records(batches, gaps)
    return result


def _clone_batch(batch: PropertyBatch, index: int) -> PropertyBatch:
    clone = PropertyBatch(index,
                          batch.interface,
                          batch.path,
                          event=batch.event,
                          split_reason=batch.split_reason)
    clone.signals = list(batch.signals)
    clone.changed = dict(batch.changed)
    clone.invalidated = dict(batch.invalidated)
    clone.notes = list(batch.notes)
    return clone


def _build_records(batches: Sequence[PropertyBatch],
                   gaps: Sequence[StreamGap]) -> List[Dict[str, Any]]:
    records = []

    for batch in batches:
        record = batch.to_dict()
        record['action'] = 'batch'
        records.append((batch.start_serial, 0, record))

    for gap in gaps:
        record = gap.to_dict()
        record['action'] = 'gap'
        records.append((gap.after_serial, 1, record))

    records.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in records]


def parse_property_change(data) -> PropertyChange:
    """Build a :class:`PropertyChange` from a mapping.

    Accepted keys are ``serial``, ``interface``, ``changed``, ``invalidated``
    (or ``invalidated_properties``), ``path``, and ``sequence``.
    """
    if isinstance(data, PropertyChange):
        return data
    if not isinstance(data, Mapping):
        raise TypeError('a property change must be a mapping or PropertyChange')

    interface = data.get('interface', data.get('interface_name'))
    if 'serial' not in data:
        raise ValueError('a property change must have a "serial"')

    invalidated = data.get('invalidated', data.get('invalidated_properties', []))
    return PropertyChange(data['serial'],
                          interface,
                          data.get('changed'),
                          invalidated,
                          path=data.get('path', '/'),
                          sequence=data.get('sequence'))


def parse_observation(data) -> ObservationPoint:
    """Build an :class:`ObservationPoint` from a mapping.

    Accepted keys are ``id``, ``interface``, ``property`` (or
    ``property_name``), ``at_serial`` (or ``serial``), ``kind``, ``path``, and
    ``after_serial``.
    """
    if isinstance(data, ObservationPoint):
        return data
    if not isinstance(data, Mapping):
        raise TypeError('an observation must be a mapping or ObservationPoint')

    property_name = data.get('property', data.get('property_name'))
    at_serial = data.get('at_serial', data.get('serial'))
    return ObservationPoint(data.get('id'),
                            data.get('interface'),
                            property_name,
                            at_serial,
                            kind=data.get('kind', ObservationKind.VALUE),
                            path=data.get('path', '/'),
                            after_serial=data.get('after_serial', 0))


def compact(signals: Iterable,
            observations: Optional[Iterable] = None,
            event_interfaces: Optional[Iterable[str]] = None,
            annotations: Optional[Mapping] = None,
            detect_gaps: bool = True,
            merge: bool = True,
            dense_serials: bool = False) -> CompactionResult:
    """Convenience wrapper around :func:`compact_property_changes` that accepts
    mappings as well as typed objects.

    Example::

        result = compact(
            signals=[
                {'serial': 1, 'interface': 'com.example.I', 'changed': {'A': 1}},
                {'serial': 2, 'interface': 'com.example.I', 'invalidated': ['B']},
            ],
            observations=[
                {'id': 'a-at-2', 'interface': 'com.example.I', 'property': 'A',
                 'at_serial': 2},
                {'id': 'b-at-2', 'interface': 'com.example.I', 'property': 'B',
                 'at_serial': 2},
            ],
        )
    """
    config = CompactionConfig(event_interfaces=event_interfaces,
                              annotations=annotations,
                              detect_gaps=detect_gaps,
                              merge=merge,
                              dense_serials=dense_serials)
    return compact_property_changes(signals, observations, config)


def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1 or (argv and argv[0] in ('-h', '--help')):
        print('usage: python -m dbus_next.property_compaction [input.json]',
              file=sys.stderr)
        return 2 if len(argv) > 1 else 0

    if argv and argv[0] != '-':
        with open(argv[0], 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    else:
        payload = json.load(sys.stdin)

    if not isinstance(payload, Mapping):
        raise ValueError('input must be a JSON object')

    result = compact(payload.get('signals', []),
                     payload.get('observations', []),
                     event_interfaces=payload.get('event_interfaces'),
                     annotations=payload.get('annotations'),
                     detect_gaps=payload.get('detect_gaps', True),
                     merge=payload.get('merge', True),
                     dense_serials=payload.get('dense_serials', False))
    json.dump(result.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
