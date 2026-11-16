"""Filtering of DBus devices/proxy objects by declarative property rules.

A rule is a boolean combination of predicates over DBus properties. Each
predicate names a property (interface and property name), an allowed set of
signatures for that property, a comparison operator and a comparison operand.

The evaluator never talks to the bus itself. It works from:

* the introspected type information (the static signature contract), and
* a map of property values that have already been read.

It returns one of three verdicts:

* :class:`FilterVerdict.MATCH`       - the device definitely satisfies the rule,
* :class:`FilterVerdict.EXCLUDE`     - the device definitely violates the rule, or
* :class:`FilterVerdict.NEED_READ`   - more properties must be read to decide.

For a ``NEED_READ`` verdict the result also carries the exact evidence that is
missing: the properties that must be fetched and, where a value is already
present but its Variant does not satisfy the rule's signature contract, the
Variant signatures that were observed.

Design rules enforced here:

* A property that has been invalidated (``PropertiesChanged`` lists it as
  invalidated) is treated as absent. A stale cached value is never substituted.
* The element type of an empty collection cannot be guessed from its elements;
  only the declared signature is authoritative.
* Integers of different widths are compared numerically after checking that the
  operand fits the property's declared integer type.
* A Variant is compared against its *inner* signature and value. A Variant whose
  inner signature is not allowed by the rule is a definite exclusion, never an
  implicit re-read (a Variant property read already carries its signature).
* Each rule carries its own signature contract. Reusing a rule object across
  evaluation batches never leaks contract state between rules.
"""

from enum import Enum
from typing import Any, List, Optional, Sequence

from .errors import RuleValidationError, SignatureBodyMismatchError
from .signature import SignatureTree, SignatureType, Variant
from .validators import is_object_path_valid

# Sentinel returned by ``_resolve_value`` when a value cannot be interpreted
# without fetching more evidence.
_NEED_READ_MARKER = object()


class FilterVerdict(Enum):
    """The three-valued result of evaluating a rule against known values."""
    MATCH = 'match'
    EXCLUDE = 'exclude'
    NEED_READ = 'need_read'


# Integer type tokens mapped to their (minimum, maximum) value ranges.
_INT_RANGES = {
    'y': (0x00, 0xff),
    'n': (-0x7fff - 1, 0x7fff),
    'q': (0, 0xffff),
    'i': (-0x7fffffff - 1, 0x7fffffff),
    'u': (0, 0xffffffff),
    'x': (-9223372036854775808, 9223372036854775807),
    't': (0, 18446744073709551615),
    'h': (0, 0xffffffff),
}

# Tokens whose values compare as text.
_STRING_TOKENS = frozenset('sog')

# All comparison operators accepted in a rule.
_OPERATORS = frozenset(('==', '!=', '<', '<=', '>', '>='))


class PropertyRef:
    """A reference to a single DBus property.

    :ivar interface: The interface that owns the property.
    :vartype interface: str
    :ivar name: The property name.
    :vartype name: str
    :ivar signatures: The signatures the rule is willing to accept for the
        property. The introspected property signature must be one of these.
    :vartype signatures: tuple(str)
    """
    __slots__ = ('interface', 'name', 'signatures')

    def __init__(self, interface: str, name: str, signatures: Sequence[str]):
        if not isinstance(interface, str) or not interface:
            raise RuleValidationError('property reference requires an interface name')
        if not isinstance(name, str) or not name:
            raise RuleValidationError('property reference requires a property name')

        normalized: List[str] = []
        for sig in signatures:
            if not isinstance(sig, str) or not sig:
                raise RuleValidationError('property signatures must be non-empty strings')
            try:
                tree = SignatureTree._get(sig)
            except Exception as err:
                raise RuleValidationError(f'invalid signature "{sig}": {err}')
            if len(tree.types) != 1:
                raise RuleValidationError(
                    f'property signature must be a single complete type (got "{sig}")')
            if sig not in normalized:
                normalized.append(sig)

        if not normalized:
            raise RuleValidationError(
                f'property {interface}.{name} must declare at least one allowed signature')

        self.interface = interface
        self.name = name
        self.signatures = tuple(normalized)

    @property
    def key(self):
        return (self.interface, self.name)

    def __eq__(self, other):
        return type(other) is PropertyRef and self.key == other.key \
            and self.signatures == other.signatures

    def __repr__(self):
        return f'PropertyRef({self.interface!r}, {self.name!r}, {list(self.signatures)!r})'


class Condition:
    """A leaf predicate: ``property op operand``.

    :ivar prop: The property the predicate reads.
    :vartype prop: :class:`PropertyRef`
    :ivar op: One of the operators in :data:`_OPERATORS`.
    :vartype op: str
    :ivar operand: The right-hand side of the comparison.
    :vartype operand: Any
    """
    __slots__ = ('prop', 'op', 'operand')

    def __init__(self, prop: PropertyRef, op: str, operand: Any):
        if type(prop) is not PropertyRef:
            raise RuleValidationError('condition property must be a PropertyRef')
        if op not in _OPERATORS:
            raise RuleValidationError(
                f'unsupported operator "{op}" (allowed: {sorted(_OPERATORS)})')
        if operand is None:
            raise RuleValidationError('comparison operand cannot be None')

        self.prop = prop
        self.op = op
        self.operand = operand
        self._validate_operand_against_contract()

    def _validate_operand_against_contract(self):
        """Validate the operand against every allowed signature.

        A rule may allow more than one signature (e.g. ``i`` or ``x``). The
        operand is valid if it is compatible with at least one allowed
        signature; it must not silently overflow any narrower type it may be
        compared under.
        """
        failures = []
        for sig in self.prop.signatures:
            try:
                self._check_operand_for_type(SignatureTree._get(sig).types[0])
                return
            except RuleValidationError as err:
                failures.append(f'{sig}: {err}')
        raise RuleValidationError(
            f'operand {self.operand!r} is not compatible with any allowed signature '
            f'{list(self.prop.signatures)}; {"; ".join(failures)}')

    def _check_operand_for_type(self, type_: SignatureType):
        token = type_.token
        if token in _INT_RANGES:
            if type(self.operand) is bool:
                raise RuleValidationError('boolean cannot be compared as an integer')
            if type(self.operand) is not int:
                raise RuleValidationError(f'expected an int for integer type "{token}"')
            minimum, maximum = _INT_RANGES[token]
            if self.operand < minimum or self.operand > maximum:
                raise RuleValidationError(
                    f'operand {self.operand} does not fit type "{token}" '
                    f'({minimum}..{maximum})')
        elif token == 'b':
            if type(self.operand) is not bool:
                raise RuleValidationError(f'expected a bool for boolean type "b"')
            if self.op not in ('==', '!='):
                raise RuleValidationError('boolean properties only support == and !=')
        elif token == 'd':
            if type(self.operand) not in (int, float) or type(self.operand) is bool:
                raise RuleValidationError('expected a number for double type "d"')
        elif token in _STRING_TOKENS:
            if type(self.operand) is not str:
                raise RuleValidationError(f'expected a str for type "{token}"')
            if token == 'o':
                if not is_object_path_valid(self.operand):
                    raise RuleValidationError(f'operand is not a valid object path: {self.operand}')
        elif token == 'a':
            if not _is_collection(self.operand):
                raise RuleValidationError(
                    f'expected a collection for array type "{type_.signature}"')
            if type_.children[0].token == '{' and self.op not in ('==', '!='):
                raise RuleValidationError('dict values only support == and != comparisons')
            self._check_collection_operand(type_, self.operand)
        elif token == '(':
            if not isinstance(self.operand, (list, tuple)):
                raise RuleValidationError('expected a list for struct type')
            if len(self.operand) != len(type_.children):
                raise RuleValidationError(
                    f'struct operand has {len(self.operand)} members, expected '
                    f'{len(type_.children)}')
            for child, member in zip(type_.children, self.operand):
                self._check_operand_value(child, member)
        elif token == 'v':
            self._check_variant_operand(self.operand)
        else:
            raise RuleValidationError(f'comparison is not supported for type "{token}"')

    def _check_collection_operand(self, array_type: SignatureType, value: Any):
        child = array_type.children[0]
        if child.token == '{':
            if not isinstance(value, dict):
                raise RuleValidationError('dict-entry array operand must be a dict')
            key_type, value_type = child.children
            for key, item in value.items():
                self._check_operand_value(key_type, key)
                self._check_operand_value(value_type, item)
        else:
            if isinstance(value, (bytes, bytearray)):
                if child.token != 'y':
                    raise RuleValidationError('bytes operand only matches an array of bytes')
                return
            if not isinstance(value, list):
                raise RuleValidationError('array operand must be a list')
            for item in value:
                self._check_operand_value(child, item)

    def _check_operand_value(self, type_: SignatureType, value: Any):
        original = self.operand
        try:
            self.operand = value
            self._check_operand_for_type(type_)
        finally:
            self.operand = original

    def _check_variant_operand(self, value: Any):
        if type(value) is not Variant:
            raise RuleValidationError(
                'operand for a Variant ("v") property must itself be a Variant')
        inner = SignatureTree._get(value.signature).types[0]
        self._check_operand_value(inner, value.value)

    def __repr__(self):
        return f'Condition({self.prop!r}, {self.op!r}, {self.operand!r})'


class _LogicNode:
    """Base class for the boolean expression tree."""

    def evaluate(self, values, invalidated, type_index, missing):
        raise NotImplementedError


class _Leaf(_LogicNode):
    __slots__ = ('condition',)

    def __init__(self, condition: Condition):
        self.condition = condition

    def evaluate(self, values, invalidated, type_index, missing):
        return _evaluate_condition(self.condition, values, invalidated, type_index, missing)


class _Not(_LogicNode):
    __slots__ = ('child',)

    def __init__(self, child: _LogicNode):
        self.child = child

    def evaluate(self, values, invalidated, type_index, missing):
        return _negate(self.child.evaluate(values, invalidated, type_index, missing))


class _And(_LogicNode):
    __slots__ = ('children',)

    def __init__(self, children: Sequence[_LogicNode]):
        self.children = children

    def evaluate(self, values, invalidated, type_index, missing):
        pending = False
        for child in self.children:
            verdict = child.evaluate(values, invalidated, type_index, missing)
            if verdict is FilterVerdict.EXCLUDE:
                return FilterVerdict.EXCLUDE
            if verdict is FilterVerdict.NEED_READ:
                pending = True
        return FilterVerdict.NEED_READ if pending else FilterVerdict.MATCH


class _Or(_LogicNode):
    __slots__ = ('children',)

    def __init__(self, children: Sequence[_LogicNode]):
        self.children = children

    def evaluate(self, values, invalidated, type_index, missing):
        pending = False
        for child in self.children:
            verdict = child.evaluate(values, invalidated, type_index, missing)
            if verdict is FilterVerdict.MATCH:
                return FilterVerdict.MATCH
            if verdict is FilterVerdict.NEED_READ:
                pending = True
        return FilterVerdict.NEED_READ if pending else FilterVerdict.EXCLUDE


def _negate(verdict: FilterVerdict) -> FilterVerdict:
    if verdict is FilterVerdict.MATCH:
        return FilterVerdict.EXCLUDE
    if verdict is FilterVerdict.EXCLUDE:
        return FilterVerdict.MATCH
    return FilterVerdict.NEED_READ


def _is_collection(value: Any) -> bool:
    return isinstance(value, (list, dict, bytes, bytearray))


class FilterResult:
    """The outcome of evaluating a :class:`PropertyFilter`.

    :ivar verdict: The three-valued verdict.
    :vartype verdict: :class:`FilterVerdict`
    :ivar missing: Properties that must be read to make further progress. Each
        item is a ``(interface, property_name)`` tuple.
    :vartype missing: list(tuple(str, str))
    :ivar signature_mismatches: Properties whose read value is a Variant with an
        inner signature not allowed by the rule's contract. Maps the property
        key to the list of observed Variant signatures.
    :vartype signature_mismatches: dict(tuple(str, str), list(str))
    """
    __slots__ = ('verdict', 'missing', 'signature_mismatches')

    def __init__(self, verdict, missing, signature_mismatches):
        self.verdict = verdict
        self.missing = missing
        self.signature_mismatches = signature_mismatches

    @property
    def needs_read(self) -> bool:
        return self.verdict is FilterVerdict.NEED_READ

    def __eq__(self, other):
        return type(other) is FilterResult \
            and self.verdict is other.verdict \
            and self.missing == other.missing \
            and self.signature_mismatches == other.signature_mismatches

    def __repr__(self):
        return (f'FilterResult({self.verdict!r}, missing={self.missing!r}, '
                f'signature_mismatches={self.signature_mismatches!r})')


class PropertyFilter:
    """A compiled, reusable property filter rule.

    Construct from a boolean expression of :class:`Condition` objects using the
    :class:`All`, :class:`Any` and :class:`Not` combinators (or plain dicts via
    :meth:`from_dict`). The rule is validated eagerly: malformed property
    references, bad signatures, unknown operators and operands incompatible
    with the declared types all raise :class:`RuleValidationError` before any
    device is inspected.

    A filter object is stateless with respect to evaluation batches: the
    signature contract lives on the rule itself and is independent per rule.
    """

    def __init__(self, expression: _LogicNode, introspection=None):
        self._root = expression
        self._refs = {}
        self._collect_refs(expression)
        self._introspection = (_normalize_introspection(introspection)
                               if introspection is not None else None)
        if self._introspection is not None:
            self._validate_against_introspection(self._introspection)

    # -- construction helpers -------------------------------------------------

    @staticmethod
    def all_of(conditions):
        """All sub-expressions must hold (boolean AND)."""
        return PropertyFilter(_And([_as_node(cond) for cond in conditions]))

    @staticmethod
    def any_of(conditions):
        """At least one sub-expression must hold (boolean OR)."""
        return PropertyFilter(_Or([_as_node(cond) for cond in conditions]))

    @staticmethod
    def not_(condition):
        """Negate a sub-expression."""
        return PropertyFilter(_Not(_as_node(condition)))

    @classmethod
    def from_dict(cls, data: dict) -> 'PropertyFilter':
        """Build a filter from a plain data structure.

        A leaf is a dict with keys ``interface``, ``name``, ``signatures``,
        ``op`` and ``operand``. Combinators are dicts with a single key
        ``all``, ``any`` or ``not``::

            {'all': [
                {'interface': 'org.example.Dev', 'name': 'Online',
                 'signatures': ['b'], 'op': '==', 'operand': True},
                {'not': {'interface': 'org.example.Dev', 'name': 'Count',
                         'signatures': ['i', 'x'], 'op': '<', 'operand': 1}},
            ]}
        """
        return cls(_node_from_dict(data))

    def with_introspection(self, introspection) -> 'PropertyFilter':
        """Return a copy of this filter validated against introspection data.

        :param introspection: A mapping of
            ``{interface: {property_name: SignatureType}}`` or, for convenience,
            a parsed :class:`dbus_next.introspection.Node`.
        """
        return PropertyFilter(self._root, introspection=_normalize_introspection(introspection))

    # -- evaluation -----------------------------------------------------------

    def evaluate(self,
                 values: Optional[dict] = None,
                 invalidated: Optional[Sequence] = None) -> FilterResult:
        """Evaluate the rule against already-read property values.

        :param values: Mapping of ``(interface, property_name)`` to the read
            value. A value read from an ``a{sv}`` property collection is
            naturally a :class:`Variant`.
        :param invalidated: Property keys that were reported invalidated since
            they were read. They are treated as absent even if a stale entry is
            present in ``values``.
        :returns: A :class:`FilterResult` with a verdict and, for
            ``NEED_READ``, the missing evidence.
        """
        values = values or {}
        invalidated = set(invalidated or ())
        type_index = self._introspection or {}

        missing: List = []
        seen_missing = set()
        signature_mismatches = {}

        verdict = self._root.evaluate(values, invalidated, type_index,
                                      _MissingSink(missing, seen_missing, signature_mismatches))

        if verdict is not FilterVerdict.NEED_READ:
            # The verdict is already settled without further reads. Evidence
            # gathered from short-circuited branches must not prompt reads of
            # properties that cannot change the outcome.
            relevant = _relevant_missing(self._root, verdict, values, invalidated, type_index)
            missing = [key for key in missing if key in relevant]
            signature_mismatches = {
                key: sigs
                for key, sigs in signature_mismatches.items() if key in relevant
            }

        return FilterResult(verdict, missing, signature_mismatches)

    @property
    def references(self):
        """The property references the rule depends on, keyed by property key."""
        return dict(self._refs)

    def required_reads(self):
        """Every property key the rule may need, regardless of short-circuiting."""
        return list(self._refs.keys())

    # -- internals ------------------------------------------------------------

    def _collect_refs(self, node: _LogicNode):
        if type(node) is _Leaf:
            ref = node.condition.prop
            existing = self._refs.get(ref.key)
            if existing is not None and existing.signatures != ref.signatures:
                raise RuleValidationError(
                    f'property {ref.interface}.{ref.name} is referenced with conflicting '
                    f'signature contracts: {list(existing.signatures)} vs '
                    f'{list(ref.signatures)}')
            self._refs[ref.key] = ref
        elif type(node) is _Not:
            self._collect_refs(node.child)
        else:
            for child in node.children:
                self._collect_refs(child)

    def _validate_against_introspection(self, type_index):
        for key, ref in self._refs.items():
            interface, name = key
            props = type_index.get(interface)
            if not props or name not in props:
                raise RuleValidationError(
                    f'property {interface}.{name} is not present in the introspection data')
            actual = props[name]
            actual_sig = actual.signature if type(actual) is SignatureType else str(actual)
            if actual_sig not in ref.signatures:
                raise RuleValidationError(
                    f'property {interface}.{name} has signature "{actual_sig}" which is not '
                    f'among the allowed signatures {list(ref.signatures)}')


class _MissingSink:
    """Accumulates missing evidence while a rule tree is evaluated."""
    __slots__ = ('missing', '_seen', 'mismatches')

    def __init__(self, missing, seen, mismatches):
        self.missing = missing
        self._seen = seen
        self.mismatches = mismatches

    def add_missing(self, key):
        if key not in self._seen:
            self._seen.add(key)
            self.missing.append(key)

    def add_mismatch(self, key, observed_signature):
        observed = self.mismatches.setdefault(key, [])
        if observed_signature not in observed:
            observed.append(observed_signature)


class _ProbeSink:
    """A sink that records every property key a branch touches."""
    __slots__ = ('keys',)

    def __init__(self):
        self.keys = set()

    def add_missing(self, key):
        self.keys.add(key)

    def add_mismatch(self, key, observed_signature):
        self.keys.add(key)


def _relevant_missing(node, target, values, invalidated, type_index):
    """Collect property keys from branches that actually decide ``target``.

    When a verdict is already settled, evidence from a short-circuited branch
    is irrelevant: e.g. an unread sibling of a failing AND term cannot change
    the exclusion. The walk follows the three-valued boolean logic recursively
    so nested combinators never leak evidence from branches that did not
    contribute to the settled verdict.
    """
    keys = set()

    def collect(current, wanted):
        kind = type(current)

        if kind is _Leaf:
            sink = _ProbeSink()
            if current.evaluate(values, invalidated, type_index, sink) is wanted:
                keys.update(sink.keys)
            return

        if kind is _Not:
            child_verdict = current.child.evaluate(values, invalidated, type_index, _ProbeSink())
            if _negate(child_verdict) is wanted:
                collect(current.child, child_verdict)
            return

        for child in current.children:
            verdict = child.evaluate(values, invalidated, type_index, _ProbeSink())
            if kind is _And:
                if wanted is FilterVerdict.MATCH:
                    collect(child, FilterVerdict.MATCH)
                elif wanted is FilterVerdict.EXCLUDE and verdict is FilterVerdict.EXCLUDE:
                    collect(child, FilterVerdict.EXCLUDE)
            else:  # _Or
                if wanted is FilterVerdict.MATCH and verdict is FilterVerdict.MATCH:
                    collect(child, FilterVerdict.MATCH)
                elif wanted is FilterVerdict.EXCLUDE:
                    collect(child, FilterVerdict.EXCLUDE)

    collect(node, target)
    return keys


def _as_node(expression) -> _LogicNode:
    if isinstance(expression, _LogicNode):
        return expression
    if type(expression) is Condition:
        return _Leaf(expression)
    if type(expression) is PropertyFilter:
        return expression._root
    raise RuleValidationError(f'cannot use {expression!r} as a rule expression')


def _node_from_dict(data: dict) -> _LogicNode:
    if not isinstance(data, dict):
        raise RuleValidationError('rule expression must be a dict')

    for combinator, wrapper in (('all', _And), ('any', _Or)):
        if combinator in data:
            if len(data) != 1:
                raise RuleValidationError(f'"{combinator}" expression must have a single key')
            children = data[combinator]
            if not isinstance(children, list) or not children:
                raise RuleValidationError(f'"{combinator}" requires a non-empty list')
            return wrapper([_node_from_dict(child) for child in children])

    if 'not' in data:
        if len(data) != 1:
            raise RuleValidationError('"not" expression must have a single key')
        return _Not(_node_from_dict(data['not']))

    return _Leaf(_condition_from_dict(data))


def _condition_from_dict(data: dict) -> Condition:
    try:
        ref = PropertyRef(data['interface'], data['name'], data['signatures'])
    except KeyError as err:
        raise RuleValidationError(f'condition is missing required key {err}')
    try:
        op = data['op']
        operand = data['operand']
    except KeyError as err:
        raise RuleValidationError(f'condition is missing required key {err}')
    return Condition(ref, op, operand)


def _normalize_introspection(introspection) -> dict:
    if isinstance(introspection, dict):
        return introspection
    # Support a parsed dbus_next.introspection.Node without importing it here.
    interfaces = getattr(introspection, 'interfaces', None)
    if interfaces is None:
        raise RuleValidationError(
            'introspection must be a {interface: {name: SignatureType}} mapping or a Node')
    index = {}
    for interface in interfaces:
        index[interface.name] = {prop.name: prop.type for prop in interface.properties}
    return index


# -- condition evaluation ------------------------------------------------------


def _evaluate_condition(condition: Condition, values, invalidated, type_index, sink):
    ref = condition.prop
    key = ref.key

    if key in invalidated:
        # An invalidated property has no trustworthy value. A stale cache entry
        # must never be substituted, so the property has to be re-read.
        sink.add_missing(key)
        return FilterVerdict.NEED_READ

    if key not in values:
        # Not read yet (or never read). Do not guess anything from other data.
        sink.add_missing(key)
        return FilterVerdict.NEED_READ

    raw_value = values[key]
    effective_type, effective_value, variant_sig = _resolve_value(ref, raw_value, type_index, key,
                                                                   sink)
    if effective_type is _NEED_READ_MARKER:
        return FilterVerdict.NEED_READ
    if effective_type is None:
        # Variant whose inner signature is not part of the rule's contract:
        # this is a definite exclusion, not a reason to re-read.
        return FilterVerdict.EXCLUDE

    try:
        holds = _compare(condition.op, effective_type, effective_value, condition.operand)
    except SignatureBodyMismatchError:
        # The read value does not conform to its own declared signature.
        return FilterVerdict.EXCLUDE

    return FilterVerdict.MATCH if holds else FilterVerdict.EXCLUDE


def _resolve_value(ref: PropertyRef, raw_value: Any, type_index, key, sink):
    """Return ``(effective SignatureType, value, variant signature)``.

    ``effective type`` is ``None`` when a Variant's inner signature is not
    allowed by the rule, which is reported as a signature mismatch and yields a
    definite exclusion.
    """
    declared = type_index.get(ref.interface, {}).get(ref.name) if type_index else None

    if type(raw_value) is Variant:
        inner_sig = raw_value.signature
        if inner_sig not in ref.signatures:
            sink.add_mismatch(key, inner_sig)
            return None, None, inner_sig
        return SignatureTree._get(inner_sig).types[0], raw_value.value, inner_sig

    if declared is not None and type(declared) is SignatureType and declared.token == 'v':
        # The property is declared as a bare Variant but a non-Variant value was
        # supplied. The signature of the value is unknowable without a re-read,
        # so treat it as missing evidence rather than guessing.
        sink.add_missing(key)
        return _NEED_READ_MARKER, None, None

    if declared is not None and type(declared) is SignatureType:
        effective_type = declared
    else:
        # No introspection: the rule's single allowed signature is the contract.
        if len(ref.signatures) != 1:
            raise RuleValidationError(
                f'property {ref.interface}.{ref.name} allows multiple signatures but no '
                'introspection type is available to disambiguate the read value')
        effective_type = SignatureTree._get(ref.signatures[0]).types[0]

    if effective_type.signature not in ref.signatures:
        sink.add_mismatch(key, effective_type.signature)
        return None, None, effective_type.signature

    return effective_type, raw_value, None


def _compare(op: str, type_: SignatureType, value: Any, operand: Any) -> bool:
    token = type_.token

    if token == 'b':
        if type(value) is not bool:
            raise SignatureBodyMismatchError('boolean property value must be a bool')
        return value == operand

    if token in _INT_RANGES:
        if type(value) is bool or type(value) is not int:
            raise SignatureBodyMismatchError(
                f'integer property value must be an int (got {type(value)})')
        minimum, maximum = _INT_RANGES[token]
        if value < minimum or value > maximum:
            raise SignatureBodyMismatchError(
                f'value {value} does not fit declared integer type "{token}"')
        return _apply_operator(op, value, operand)

    if token == 'd':
        if type(value) not in (int, float) or type(value) is bool:
            raise SignatureBodyMismatchError('double property value must be a number')
        return _apply_operator(op, float(value), float(operand))

    if token in _STRING_TOKENS:
        if type(value) is not str:
            raise SignatureBodyMismatchError(f'property value for "{token}" must be a str')
        return _apply_operator(op, value, operand)

    if token == 'a':
        return _compare_collection(op, type_, value, operand)

    if token == '(':
        if not isinstance(value, list) or len(value) != len(type_.children):
            raise SignatureBodyMismatchError('struct value does not match its signature')
        return _apply_operator(op, value, list(operand))

    raise RuleValidationError(f'comparison is not supported for type "{token}"')


def _compare_collection(op: str, array_type: SignatureType, value: Any, operand: Any) -> bool:
    child = array_type.children[0]

    if child.token == '{':
        if not isinstance(value, dict) or not isinstance(operand, dict):
            raise SignatureBodyMismatchError('dict-entry property value must be a dict')
        if op in ('==', '!='):
            return _apply_operator(op, value, operand)
        # Ordering on whole dicts is not defined; only membership-style use is
        # meaningful, which is validated at rule construction time.
        raise RuleValidationError(f'operator "{op}" is not supported on dict values')

    if isinstance(value, (bytes, bytearray)):
        if child.token != 'y':
            raise SignatureBodyMismatchError('bytes value only matches an array of bytes')
        return _apply_operator(op, bytes(value), operand)

    if not isinstance(value, list):
        raise SignatureBodyMismatchError('array property value must be a list')

    if op in ('==', '!='):
        return _apply_operator(op, value, operand)

    # Ordering operators on arrays compare element-wise by length then members,
    # mirroring Python sequence ordering. An empty array is fully comparable
    # without ever inspecting (non-existent) elements.
    return _apply_operator(op, value, operand)


def _apply_operator(op: str, left: Any, right: Any) -> bool:
    if op == '==':
        return left == right
    if op == '!=':
        return left != right
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    if op == '>':
        return left > right
    if op == '>=':
        return left >= right
    raise RuleValidationError(f'unsupported operator "{op}"')
