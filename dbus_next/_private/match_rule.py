"""Semantic representation of a D-Bus match rule.

The D-Bus daemon compares match rules by their meaning, not by the exact
string that was passed to ``AddMatch``.  For example, the following two
rules describe the same subscription and must therefore only be registered
with the bus once::

    type='signal',path='/com/example'
    path='/com/example',type='signal'

Likewise, quoting and escaping in a rule are only syntax.  This module
implements a parser that normalizes rules into semantic conditions and can
test incoming messages against them.

The implementation follows the match rule section of the D-Bus
specification:
https://dbus.freedesktop.org/doc/dbus-specification.html#message-bus-routing-match-rules
"""

from ..constants import MessageType
from ..errors import InvalidMatchRuleError
from ..validators import (is_bus_name_valid, is_interface_name_valid,
                          is_member_name_valid, is_object_path_valid)

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union


# Kinds of argument conditions.  ``argN`` matches an exact string,
# ``argNpath`` uses object path namespace semantics and ``argNnamespace``
# uses dot separated namespace semantics.
ARG_STRING = 'string'
ARG_PATH = 'path'
ARG_NAMESPACE = 'namespace'

_ARG_KIND_SUFFIXES = (
    ('path', ARG_PATH),
    ('namespace', ARG_NAMESPACE),
)


def _path_namespace_match(namespace: str, path: Optional[str]) -> bool:
    """Object path namespace semantics used by path_namespace and argNpath."""
    if not isinstance(path, str):
        return False
    if namespace == '/':
        return path.startswith('/')
    return path == namespace or path.startswith(namespace + '/')


def _string_namespace_match(namespace: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if namespace == '':
        return True
    # arg0namespace matches either the namespace itself or a member of it:
    # the string must equal the namespace or continue with a dot.  A longer
    # element with the same prefix does not match.
    return value == namespace or value.startswith(namespace + '.')


def _parse_arg_key(key: str) -> Optional[Tuple[int, str]]:
    """Return ``(index, kind)`` for an argument condition key.

    Returns ``None`` for keys which are not argument conditions.
    """
    if not key.startswith('arg'):
        return None

    rest = key[3:]
    if not rest:
        return None

    kind = ARG_STRING
    for suffix, suffix_kind in _ARG_KIND_SUFFIXES:
        if rest.endswith(suffix):
            kind = suffix_kind
            rest = rest[:-len(suffix)]
            break

    if not rest.isdigit():
        return None

    index = int(rest)
    if index < 0 or index > 63:
        raise InvalidMatchRuleError(f'argument index out of range in match rule key: {key!r}')
    if kind == ARG_NAMESPACE and index != 0:
        raise InvalidMatchRuleError(
            f'only arg0namespace is supported by the D-Bus specification: {key!r}')
    return index, kind


def _unescape_value(value: str) -> str:
    """Interpret quoting and escaping in a match rule value.

    Following the bus parser, adjacent single-quoted and unquoted sections
    are concatenated and the quotes are removed.  A backslash escapes the
    following character only inside a quoted section.
    """
    result = []
    quoted = False
    escaped = False
    i = 0
    while i < len(value):
        char = value[i]
        if escaped:
            result.append(char)
            escaped = False
            i += 1
            continue
        if char == '\\' and quoted:
            escaped = True
            i += 1
            continue
        if char == "'":
            quoted = not quoted
            i += 1
            continue
        result.append(char)
        i += 1
    if quoted:
        raise InvalidMatchRuleError(f'unterminated quoted value in match rule: {value!r}')
    return ''.join(result)


def _escape_value(value: str) -> str:
    return value.replace('\\', '\\\\').replace("'", "\\'")


def _split_rule(rule: str) -> Iterable[str]:
    """Split a rule on commas that are not inside a quoted value."""
    token = []
    quoted = False
    escaped = False
    for char in rule:
        if escaped:
            token.append(char)
            escaped = False
            continue
        if char == '\\' and quoted:
            token.append(char)
            escaped = True
            continue
        if char == "'":
            quoted = not quoted
            token.append(char)
            continue
        if char == ',' and not quoted:
            if ''.join(token).strip():
                yield ''.join(token)
            token = []
            continue
        token.append(char)

    if quoted:
        raise InvalidMatchRuleError(f'unterminated quoted value in match rule: {rule!r}')
    if ''.join(token).strip():
        yield ''.join(token)


def _split_key_value(token: str) -> Tuple[str, str]:
    quoted = False
    escaped = False
    for i, char in enumerate(token):
        if escaped:
            escaped = False
            continue
        if char == '\\' and quoted:
            escaped = True
            continue
        if char == "'":
            quoted = not quoted
            continue
        if char == '=' and not quoted:
            key = token[:i].strip()
            value = token[i + 1:]
            if not key:
                raise InvalidMatchRuleError(f'match rule token has no key: {token!r}')
            return key, value
    raise InvalidMatchRuleError(f'match rule token has no value: {token!r}')


class _ArgList(list):
    """A list of ``(index, value)`` argument conditions.

    This behaves like a list for compatibility with code that treats match
    rule arguments as an ordered sequence, but also supports lookup by
    argument index for convenience.
    """
    def get_for_index(self, index: int) -> Optional[str]:
        for arg_index, value in self:
            if arg_index == index:
                return value
        return None

    def as_dict(self) -> Dict[int, str]:
        return dict(self)

    def __eq__(self, other):
        if isinstance(other, Mapping):
            return self.as_dict() == dict(other)
        return super().__eq__(other)

    __hash__ = None


class _ArgConditions:
    """Internal immutable description of all argument conditions."""
    __slots__ = ('conditions', )

    def __init__(self, conditions: Mapping[Tuple[int, str], str]):
        self.conditions = dict(conditions)

    def __eq__(self, other):
        if isinstance(other, _ArgConditions):
            return self.conditions == other.conditions
        return NotImplemented

    def __hash__(self):
        return hash(tuple(sorted(self.conditions.items())))


class MatchRule:
    """A parsed, semantic D-Bus match rule.

    Rules compare and hash by meaning.  Parsing the same subscription written
    with a different key order, different quoting, or redundant whitespace
    produces equal objects, which allows a message bus to register a single
    rule with the daemon for many consumers.

    :ivar type: If set, restrict matches to this message type.
    :vartype type: :class:`MessageType <dbus_next.MessageType>` or None
    :ivar sender: If set, match this sender.  A well-known name is resolved
        using the bus name owner cache when matching messages.
    :ivar interface: If set, match this interface name.
    :ivar member: If set, match this member name.
    :ivar path: If set, match exactly this object path.
    :ivar path_namespace: If set, match this object path or any child of it.
    :ivar destination: If set, match this destination.
    :ivar eavesdrop: Whether the rule was registered with eavesdrop enabled.
    :ivar args: A list of ``(index, value)`` conditions for ``argN`` keys.
    :ivar arg_paths: A mapping of index to value for ``argNpath`` keys.
    :ivar arg_namespaces: A mapping of index to value for ``argNnamespace``.
    """
    def __init__(self,
                 type: Optional[Union[MessageType, str, int]] = None,
                 sender: Optional[str] = None,
                 interface: Optional[str] = None,
                 member: Optional[str] = None,
                 path: Optional[str] = None,
                 path_namespace: Optional[str] = None,
                 destination: Optional[str] = None,
                 eavesdrop: bool = False,
                 args: Optional[Union[Mapping[int, Any], Iterable[Any]]] = None,
                 arg0namespace: Optional[str] = None,
                 arg_paths: Optional[Mapping[int, str]] = None,
                 arg_namespaces: Optional[Mapping[int, str]] = None,
                 **kwargs):
        if type is None:
            type = kwargs.pop('message_type', None)

        if type is not None and not isinstance(type, MessageType):
            if isinstance(type, str):
                try:
                    type = MessageType[type.upper()]
                except KeyError:
                    raise InvalidMatchRuleError(f'invalid message type in match rule: {type!r}')
            else:
                try:
                    type = MessageType(type)
                except ValueError:
                    raise InvalidMatchRuleError(f'invalid message type in match rule: {type!r}')

        if sender is not None and not is_bus_name_valid(sender):
            raise InvalidMatchRuleError(f'invalid sender in match rule: {sender!r}')
        if destination is not None and not is_bus_name_valid(destination):
            raise InvalidMatchRuleError(f'invalid destination in match rule: {destination!r}')
        if interface is not None and not is_interface_name_valid(interface):
            raise InvalidMatchRuleError(f'invalid interface in match rule: {interface!r}')
        if member is not None and not is_member_name_valid(member):
            raise InvalidMatchRuleError(f'invalid member in match rule: {member!r}')
        if path is not None and not is_object_path_valid(path):
            raise InvalidMatchRuleError(f'invalid path in match rule: {path!r}')
        if path_namespace is not None and not is_object_path_valid(path_namespace):
            raise InvalidMatchRuleError(
                f'invalid path_namespace in match rule: {path_namespace!r}')

        self.type = type
        self.sender = sender
        self.interface = interface
        self.member = member
        self.path = path
        self.path_namespace = path_namespace
        self.destination = destination
        self.eavesdrop = bool(eavesdrop)

        # Conditions are keyed by (argument index, condition kind), preserving
        # the fact that argN, argNpath and argNnamespace for the same index are
        # independent conditions which must all match.
        conditions: Dict[Tuple[int, str], str] = {}

        if args is not None:
            if isinstance(args, Mapping):
                for index, condition in args.items():
                    self._add_condition(conditions, int(index), condition)
            else:
                for condition in args:
                    self._add_condition_object(conditions, condition)

        if arg_paths:
            for index, value in arg_paths.items():
                self._set_condition(conditions, int(index), ARG_PATH, value)
        if arg_namespaces:
            for index, value in arg_namespaces.items():
                self._set_condition(conditions, int(index), ARG_NAMESPACE, value)
        if arg0namespace is not None:
            self._set_condition(conditions, 0, ARG_NAMESPACE, arg0namespace)

        # Accept argN, argNpath and argNnamespace constructor keyword
        # arguments for convenience.
        for key, value in list(kwargs.items()):
            parsed = _parse_arg_key(key)
            if parsed is None:
                raise TypeError(f'unexpected match rule field: {key}')
            self._set_condition(conditions, parsed[0], parsed[1], value)

        self._conditions = _ArgConditions(conditions)
        self._rebuild_views()

    @staticmethod
    def _set_condition(conditions, index: int, kind: str, value: Any) -> None:
        if index < 0 or index > 63:
            raise InvalidMatchRuleError(f'argument index out of range: {index}')
        if kind == ARG_NAMESPACE and index != 0:
            raise InvalidMatchRuleError(
                f'only arg0namespace is supported by the D-Bus specification: arg{index}namespace')
        if not isinstance(value, str):
            raise InvalidMatchRuleError(
                f'argument condition values must be strings: {value!r}')
        if kind == ARG_PATH and not is_object_path_valid(value):
            raise InvalidMatchRuleError(
                f'invalid object path for argNpath condition: {value!r}')
        conditions[(index, kind)] = value

    @classmethod
    def _add_condition(cls, conditions, index: int, condition: Any) -> None:
        if isinstance(condition, _ArgCondition):
            cls._set_condition(conditions, condition.index, condition.kind, condition.value)
        elif isinstance(condition, str):
            cls._set_condition(conditions, index, ARG_STRING, condition)
        elif isinstance(condition, tuple):
            if len(condition) == 2:
                cls._set_condition(conditions, int(condition[0]), ARG_STRING,
                                   str(condition[1]))
            elif len(condition) == 3:
                index_, kind_, value_ = condition
                cls._set_condition(conditions, int(index_), str(kind_), str(value_))
            else:
                raise TypeError(f'invalid match rule argument condition: {condition!r}')
        elif isinstance(condition, Mapping):
            kind = condition.get('kind', ARG_STRING)
            index = int(condition.get('index'))
            value = condition.get('value')
            cls._set_condition(conditions, index, kind, value)
        else:
            raise TypeError(f'invalid match rule argument condition: {condition!r}')

    @classmethod
    def _add_condition_object(cls, conditions, condition: Any) -> None:
        if isinstance(condition, (_ArgCondition, str, tuple, Mapping)):
            # A sequence of conditions carries its own index, so the index
            # argument is unused here.
            cls._add_condition(conditions, 0, condition)
        else:
            raise TypeError(f'invalid match rule argument condition: {condition!r}')

    @classmethod
    def from_string(cls, rule: Union[str, 'MatchRule'], **overrides) -> 'MatchRule':
        """Parse a match rule string into a :class:`MatchRule`.

        Additional keyword arguments are applied after parsing, which is
        convenient for restricting a caller supplied rule to signals.

        :param rule: The match rule as passed to ``AddMatch``.
        :raises: :class:`InvalidMatchRuleError <dbus_next.InvalidMatchRuleError>`
        """
        if isinstance(rule, MatchRule):
            if not overrides:
                return rule
            fields = {key: value for key, value in rule.to_kwargs().items()
                      if key not in ('args', 'arg_paths', 'arg_namespaces')}
            fields['args'] = list(rule.args)
            fields['arg_paths'] = dict(rule.arg_paths)
            fields['arg_namespaces'] = dict(rule.arg_namespaces)
            fields.update(overrides)
            return cls(**fields)
        if not isinstance(rule, str):
            raise TypeError('match rule must be a string or MatchRule')

        fields: Dict[str, Any] = {}
        args: List[Tuple[int, str, str]] = []

        for token in _split_rule(rule):
            key, raw_value = _split_key_value(token)
            value = _unescape_value(raw_value)

            arg_key = _parse_arg_key(key)
            if arg_key is not None:
                args.append((arg_key[0], arg_key[1], value))
                continue

            if key == 'type':
                try:
                    fields['type'] = MessageType[value.upper()]
                except KeyError:
                    raise InvalidMatchRuleError(
                        f'invalid message type in match rule: {value!r}')
            elif key in ('sender', 'interface', 'member', 'path', 'path_namespace',
                         'destination'):
                fields[key] = value
            elif key == 'eavesdrop':
                if value.lower() in ('true', '1', 'yes'):
                    fields['eavesdrop'] = True
                elif value.lower() in ('false', '0', 'no'):
                    fields['eavesdrop'] = False
                else:
                    raise InvalidMatchRuleError(
                        f'invalid eavesdrop value in match rule: {value!r}')
            else:
                raise InvalidMatchRuleError(f'unknown match rule key: {key!r}')

        rule_obj = cls(**fields)
        conditions = dict(rule_obj._conditions.conditions)
        for index, kind, value in args:
            cls._set_condition(conditions, index, kind, value)
        rule_obj._conditions = _ArgConditions(conditions)

        if overrides:
            for key, value in overrides.items():
                if key in ('args', 'arg_paths', 'arg_namespaces', 'arg0namespace'):
                    raise TypeError(
                        f'argument conditions cannot be overridden separately: {key}')
                if key == 'type':
                    rule_obj.type = cls(type=value).type
                elif hasattr(rule_obj, key):
                    setattr(rule_obj, key, value)
                else:
                    raise TypeError(f'unexpected match rule field: {key}')

        rule_obj._rebuild_views()
        return rule_obj

    def _rebuild_views(self) -> None:
        self.args = _ArgList()
        self.arg_paths = {}
        self.arg_namespaces = {}
        for (index, kind), value in sorted(self._conditions.conditions.items()):
            if kind == ARG_STRING:
                self.args.append((index, value))
            elif kind == ARG_PATH:
                self.arg_paths[index] = value
            elif kind == ARG_NAMESPACE:
                self.arg_namespaces[index] = value

    def to_kwargs(self) -> Dict[str, Any]:
        """Return constructor keyword arguments representing this rule."""
        fields = {
            'type': self.type,
            'sender': self.sender,
            'interface': self.interface,
            'member': self.member,
            'path': self.path,
            'path_namespace': self.path_namespace,
            'destination': self.destination,
            'eavesdrop': self.eavesdrop,
            'args': list(self.args),
            'arg_paths': dict(self.arg_paths),
            'arg_namespaces': dict(self.arg_namespaces),
        }
        return {key: value for key, value in fields.items() if value is not None}

    # Common aliases used by callers that prefer explicit names.
    parse = from_string
    from_str = from_string

    @property
    def arg0namespace(self) -> Optional[str]:
        return self.arg_namespaces.get(0)

    @property
    def message_type(self) -> Optional[MessageType]:
        return self.type

    def _semantic_key(self):
        # path_namespace='/' matches every path, but it still requires the
        # message to have one, so it is not equivalent to leaving the field
        # out.
        return (
            self.type.value if self.type is not None else None,
            self.sender,
            self.interface,
            self.member,
            self.path,
            self.path_namespace,
            self.destination,
            tuple(sorted(self._conditions.conditions.items())),
            bool(self.eavesdrop),
        )

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            try:
                other = MatchRule.from_string(other)
            except (InvalidMatchRuleError, TypeError):
                return False
        if not isinstance(other, MatchRule):
            return NotImplemented
        return self._semantic_key() == other._semantic_key()

    def __hash__(self) -> int:
        return hash(self._semantic_key())

    def __repr__(self) -> str:
        return f'MatchRule({self.to_string()!r})'

    def to_string(self) -> str:
        """Serialize this rule in the daemon's canonical field order."""
        parts = []

        def add(key, value):
            parts.append(f"{key}='{_escape_value(str(value))}'")

        if self.type is not None:
            add('type', self.type.name.lower())
        if self.interface is not None:
            add('interface', self.interface)
        if self.member is not None:
            add('member', self.member)
        if self.path is not None:
            add('path', self.path)
        if self.path_namespace is not None:
            add('path_namespace', self.path_namespace)
        if self.sender is not None:
            add('sender', self.sender)
        if self.destination is not None:
            add('destination', self.destination)

        conditions = self._conditions.conditions
        for (index, kind), value in sorted(conditions.items()):
            if kind == ARG_STRING:
                add(f'arg{index}', value)
        for (index, kind), value in sorted(conditions.items()):
            if kind == ARG_PATH:
                add(f'arg{index}path', value)
        for (index, kind), value in sorted(conditions.items()):
            if kind == ARG_NAMESPACE:
                add(f'arg{index}namespace', value)

        if self.eavesdrop:
            add('eavesdrop', 'true')

        return ','.join(parts)

    __str__ = to_string

    def matches(self,
                message,
                name_owners: Optional[Mapping[str, str]] = None,
                bus=None) -> bool:
        """Return whether ``message`` is matched by this rule.

        :param message: The message to test.
        :type message: :class:`Message <dbus_next.Message>`
        :param name_owners: A mapping of well-known bus names to the unique
            name of their current owner, used for the ``sender`` condition.
        :param bus: An optional message bus whose name owner cache should be
            used.  ``name_owners`` takes precedence when given.
        """
        if self.type is not None and message.message_type != self.type:
            return False

        if self.interface is not None and message.interface != self.interface:
            return False

        if self.member is not None and message.member != self.member:
            return False

        if self.path is not None and message.path != self.path:
            return False

        if self.path_namespace is not None and not _path_namespace_match(
                self.path_namespace, message.path):
            return False

        if self.destination is not None and message.destination != self.destination:
            return False

        if self.sender is not None:
            sender = message.sender
            if sender != self.sender:
                owners = name_owners
                if owners is None and bus is not None:
                    owners = getattr(bus, '_name_owners', None)
                if not owners:
                    return False
                owner = owners.get(self.sender)
                if owner is None or owner != sender:
                    return False

        conditions = self._conditions.conditions
        if conditions:
            body = message.body or []
            tree = getattr(message, 'signature_tree', None)
            tokens = [t.token for t in tree.types] if tree is not None else None

            for (index, kind), expected in conditions.items():
                if index >= len(body):
                    return False
                value = body[index]
                if not isinstance(value, str):
                    return False

                if kind == ARG_STRING:
                    if tokens is not None and index < len(tokens) and tokens[index] != 's':
                        return False
                    if value != expected:
                        return False
                elif kind == ARG_PATH:
                    if tokens is not None and index < len(tokens) and tokens[index] != 'o':
                        return False
                    if not _path_namespace_match(expected, value):
                        return False
                elif kind == ARG_NAMESPACE:
                    if tokens is not None and index < len(tokens) and tokens[index] != 's':
                        return False
                    if not _string_namespace_match(expected, value):
                        return False
                else:
                    return False

        return True

    # ------------------------------------------------------------------
    # Convenience constructors mirroring the standard D-Bus keys.
    # ------------------------------------------------------------------
    @classmethod
    def signal(cls, **kwargs) -> 'MatchRule':
        kwargs['type'] = MessageType.SIGNAL
        return cls(**kwargs)


class _ArgCondition:
    """A helper for constructing argument conditions."""
    __slots__ = ('index', 'kind', 'value')

    def __init__(self, index: int, value: str, kind: str = ARG_STRING):
        self.index = index
        self.kind = kind
        self.value = value


# Alias used by some callers.
_SemanticArg = _ArgCondition


def arg(index: int, value: str) -> _ArgCondition:
    return _ArgCondition(index, value, ARG_STRING)


def arg_path(index: int, value: str) -> _ArgCondition:
    return _ArgCondition(index, value, ARG_PATH)


def arg_namespace(index: int, value: str) -> _ArgCondition:
    return _ArgCondition(index, value, ARG_NAMESPACE)


def parse_match_rule(rule: Union[str, MatchRule]) -> MatchRule:
    """Module level convenience wrapper around :meth:`MatchRule.from_string`."""
    return MatchRule.from_string(rule)
