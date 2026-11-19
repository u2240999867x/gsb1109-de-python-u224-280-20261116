"""Replay CBOR records against changed tag interpretation rules.

This module answers a narrow but important question: after the rules for
interpreting CBOR tags change, which fields must be interpreted again, and
what do they become?  The original tag, the original bytes and the rule that
was used to produce every value stay available for audit.

The engine is intentionally independent of :class:`cbor2.CBORDecoder`.  A
"record" is either a :class:`cbor2.CBORTag`, an annotated
:class:`TaggedValue`, or a nested Python container of those.  Handlers are
plain callables receiving a :class:`ReplayContext`; they only run for tags
that were replayed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from typing import Any

from ._encoder import CBOREncoder
from ._rules import RuleChange, RuleSet, RuleSetDiff, TagRule
from ._types import CBORError, CBORTag

Handler = Callable[["ReplayContext"], Any]


def _is_tag(value: Any) -> bool:
    """Whether *value* is a tag from either the Python or the C implementation."""

    return isinstance(value, (TaggedValue, CBORTag)) or (
        type(value).__name__ == "CBORTag" and hasattr(value, "tag") and hasattr(value, "value")
    )


class CBORReplayError(CBORError):
    """Raised when a record cannot be replayed safely."""


class ValueState(str, Enum):
    """Outcome of interpreting one portion of a record."""

    #: The value was successfully interpreted under a rule set
    INTERPRETED = "interpreted"
    #: No rule exists for the tag, so it remains an opaque tag
    OPAQUE = "opaque"
    #: A rule exists but the value could not be fully interpreted
    PENDING = "pending"


@dataclass(frozen=True, unsafe_hash=True)
class TaggedValue:
    """A tagged value together with the raw bytes it was decoded from.

    The raw bytes are required for ``"raw"`` handlers.  They are also what
    makes the original payload auditable even after the value is replaced by
    an interpreted result.

    :param tag: the CBOR tag number
    :param payload: the decoded payload.  May be raw bytes when the payload
        was never decoded
    :param raw: the original encoded payload bytes, if known
    :param source: free form provenance information (for example a field
        path or record identifier)
    :param previous: the value the tag was previously interpreted to, if it
        is known.  Used as the nested value when an outer handler is replayed
        but the nested rule did not change
    """

    tag: int
    payload: Any = None
    raw: bytes | None = None
    source: str | None = None
    previous: Any = None

    @classmethod
    def from_tag(
        cls, tag: CBORTag, raw: bytes | None = None, source: str | None = None
    ) -> TaggedValue:
        return cls(tag.tag, tag.value, raw, source)

    def to_tag(self) -> CBORTag:
        """Return an ordinary :class:`cbor2.CBORTag` for this value."""

        return CBORTag(self.tag, self.payload)

    @property
    def prior_value(self) -> Any:
        """The value to use for an unchanged nested tag under a new outer rule."""

        return self.previous if self.previous is not None else self.payload


class _PendingPayload:
    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"Pending({self.reason!r})"


@dataclass
class Node:
    """A single tagged node in the attribution tree."""

    tag: int
    state: ValueState
    rule: TagRule | None = None
    previous_rule: TagRule | None = None
    old_value: Any = None
    new_value: Any = None
    decoded: Any = None
    replayed: bool = False
    retagged: bool = False
    reason: str | None = None
    inputs: list[Node] = field(default_factory=list)
    raw: bytes | None = None
    tag_raw: bytes | None = None
    retagged_tag: int | None = None
    source: str | None = None
    original: Any = None

    @property
    def new_tag(self) -> int:
        """The tag number of the current value (may differ after retagging)."""

        return self.retagged_tag if self.retagged_tag is not None else self.tag

    @property
    def pending(self) -> bool:
        return self.state is ValueState.PENDING

    @property
    def changed(self) -> bool:
        return self.retagged or (self.replayed and self.new_value != self.old_value)

    @property
    def affected(self) -> bool:
        """Whether this tag was interpreted again (regardless of the result)."""

        return self.replayed or self.retagged

    @property
    def value(self) -> Any:
        """Return the value to expose for this node.

        Values that were not interpreted again keep their original object, so
        an unresolved field never degrades a tag into a string.  The original
        tag and bytes remain available through :attr:`tag`, :attr:`raw` and
        :attr:`tag_raw`.
        """

        if self.state is ValueState.INTERPRETED:
            return self.new_value

        return self.original

    def walk(self) -> Iterator[Node]:
        """Yield this node and all nested nodes depth first."""

        yield self
        for child in self.inputs:
            yield from child.walk()


@dataclass
class FieldResult:
    """The replay outcome for one field of a record."""

    path: str
    status: str
    old_value: Any = None
    new_value: Any = None
    old_tag: int | None = None
    new_tag: int | None = None
    rule_version: str | None = None
    old_rule: TagRule | None = None
    new_rule: TagRule | None = None
    reason: str | None = None
    nodes: list[Node] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.status == "reinterpreted"

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_tag": self.old_tag,
            "new_tag": self.new_tag,
            "rule_version": self.rule_version,
            "old_rule": self.old_rule.to_dict() if self.old_rule else None,
            "new_rule": self.new_rule.to_dict() if self.new_rule else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Dependency:
    """An edge in the dependency closure used for a replay."""

    tag: int
    path: str
    reason: str
    change: RuleChange


class ReplayContext:
    """Context handed to tag handlers during replay.

    :param replay: the replay operation
    :param rule: the rule being applied
    :param node: the attribution node for the tag
    :param payload: the payload presented to the handler
    :param rule_version: the rule set version
    """

    __slots__ = ("replay", "rule", "node", "payload", "rule_version")

    def __init__(
        self,
        replay: Replay,
        rule: TagRule,
        node: Node,
        payload: Any,
        rule_version: str,
    ) -> None:
        self.replay = replay
        self.rule = rule
        self.node = node
        self.payload = payload
        self.rule_version = rule_version

    @property
    def tag(self) -> int:
        return self.rule.tag

    @property
    def raw(self) -> bytes | None:
        """The original payload bytes, if available."""

        return self.node.raw

    @property
    def source(self) -> str | None:
        return self.node.source

    def pending(self, reason: str) -> Any:
        """Mark the current field as pending because its path is incomplete.

        The handler may return this sentinel; the replay then keeps the
        previous value and records the reason.  The original tag and bytes are
        still reported.
        """

        self.node.reason = reason
        return _PendingPayload(reason)

    def interpret(self, value: Any) -> Any:
        """Interpret a nested payload under the new rule set."""

        new_value, _ = self.replay._replay_payload(value, self.rule, self.node.source)
        return new_value


@dataclass
class RecordResult:
    """The result of replaying a record."""

    old: Any
    new: Any
    fields: list[FieldResult] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    rule_version: str | None = None
    old_rule_version: str | None = None
    nodes: list[Node] = field(default_factory=list)

    @property
    def replayed(self) -> bool:
        """Whether any field was interpreted again."""

        return any(field.status == "reinterpreted" for field in self.fields)

    @property
    def changed(self) -> bool:
        """Whether any field's value changed."""

        return any(field.changed for field in self.fields)

    @property
    def pending(self) -> list[FieldResult]:
        """Fields that could not be completely interpreted."""

        return [field for field in self.fields if field.pending]

    @property
    def replayed_fields(self) -> list[FieldResult]:
        return [field for field in self.fields if field.status == "reinterpreted"]

    def field(self, path: str) -> FieldResult:
        for result in self.fields:
            if result.path == path:
                return result

        raise KeyError(path)

    def __getitem__(self, path: str) -> FieldResult:
        return self.field(path)


def _join_path(prefix: str, key: Any) -> str:
    if prefix:
        return f"{prefix}.{key}"

    return str(key)


def _tag_bytes(tag: int) -> bytes:
    """Encode a tag number the way the CBOR encoder would."""

    with BytesIO() as fp:
        encoder = CBOREncoder(fp)
        encoder.encode_length(6, tag)
        return fp.getvalue()


class Replay:
    """Replay records against a change of tag interpretation rules.

    :param old_rules: the rule set used to produce the recorded values
    :param new_rules: the rule set that must now be applied
    :param handlers: mapping of semantic names to handler callables
    :param decode: optional callable turning raw payload bytes into a value.
        It is given ``(bytes, tag)`` and is only called for replayed tags whose
        handler receives interpreted input.
    """

    def __init__(
        self,
        old_rules: RuleSet,
        new_rules: RuleSet,
        handlers: Mapping[str, Handler] | None = None,
        decode: Callable[[bytes, int], Any] | None = None,
    ) -> None:
        self.old_rules = old_rules
        self.new_rules = new_rules
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.decode = decode
        self.diff: RuleSetDiff = old_rules.diff(new_rules)
        self._closure_cache: dict[int, frozenset[int]] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register or replace a semantic handler."""

        if not isinstance(name, str) or not name:
            raise CBORReplayError("handler name must be a non-empty string")

        if not callable(handler):
            raise CBORReplayError("handler must be callable")

        self.handlers[name] = handler

    def dependency_closure(self, tag: int) -> frozenset[int]:
        """Return *tag* and every tag that can influence its interpretation.

        The closure follows declared dependencies of the *new* rule set.  It
        also follows dependencies that the *old* rule used: if an inner tag
        stops being interpreted, an outer interpreted value that was derived
        from it must be produced again as well.
        """

        if tag in self._closure_cache:
            return self._closure_cache[tag]

        result: set[int] = set()
        stack = [tag]
        while stack:
            current = stack.pop()
            if current in result:
                continue

            result.add(current)
            for rules in (self.new_rules, self.old_rules):
                rule = rules.get(current)
                if rule is None or rule.dependencies == "all":
                    # "all" means every nested tag; the actual set is only
                    # known per record, so callers must combine this with the
                    # per-node dependency list.
                    continue

                stack.extend(rule.dependencies)

        closure = frozenset(result)
        self._closure_cache[tag] = closure
        return closure

    def needs_replay(self, tag: int) -> bool:
        """Whether *tag* itself changed between the rule sets."""

        return self.diff.affects_tag(tag)

    def is_affected(self, tag: int, nested_tags: Sequence[int] = ()) -> bool:
        """Whether *tag* or a declared dependency changed."""

        closure = set(self.dependency_closure(tag))
        closure.update(nested_tags)
        return any(self.diff.affects_tag(item) for item in closure)

    def replay(
        self,
        record: Any,
        *,
        old_value: Any = None,
        source: str | None = None,
    ) -> RecordResult:
        """Replay a decoded *record*.

        ``record`` may be a :class:`TaggedValue`, a :class:`cbor2.CBORTag`, or
        a mapping/list/tuple containing them.  ``old_value`` is the previously
        recorded result; when omitted the tagged values themselves are used.
        """

        if old_value is None:
            old_value = self._materialise(record)

        new_record, top_nodes = self._walk(record, source or "")
        nodes: list[Node] = []
        dependencies: list[Dependency] = []
        seen_dependencies: set[tuple[int, str, str]] = set()
        for node in top_nodes:
            for item in node.walk():
                nodes.append(item)

            for dependency in self._dependencies(node, seen_dependencies):
                dependencies.append(dependency)

        fields = self._fields(top_nodes)
        return RecordResult(
            old=old_value,
            new=new_record,
            fields=fields,
            dependencies=dependencies,
            rule_version=self.new_rules.version,
            old_rule_version=self.old_rules.version,
            nodes=nodes,
        )

    def _dependencies(self, node: Node, seen: set[tuple[int, str, str]]) -> Iterator[Dependency]:
        for item in node.walk():
            change = self.diff.changes.get(item.tag)
            if change is None or not change.affects_interpretation:
                continue

            if not item.replayed:
                continue

            key = (item.tag, item.source or "", change.kind)
            if key in seen:
                continue

            seen.add(key)
            yield Dependency(
                tag=item.tag,
                path=item.source or "",
                reason=_change_reason(change),
                change=change,
            )

    def _fields(self, nodes: list[Node]) -> list[FieldResult]:
        fields: list[FieldResult] = []
        for node in nodes:
            if node.state is ValueState.PENDING:
                status = "pending"
            elif node.replayed:
                status = "reinterpreted"
            else:
                status = "unchanged"

            fields.append(
                FieldResult(
                    path=node.source or "",
                    status=status,
                    old_value=node.old_value,
                    new_value=node.value,
                    old_tag=node.tag,
                    new_tag=node.new_tag,
                    rule_version=self.new_rules.version,
                    old_rule=node.previous_rule,
                    new_rule=node.rule,
                    reason=node.reason,
                    nodes=list(node.walk()),
                )
            )

        fields.sort(key=lambda item: item.path)
        return fields

    def _walk(self, value: Any, path: str) -> tuple[Any, list[Node]]:
        if _is_tag(value):
            new_value, node = self._replay_tag(value, path)
            return new_value, [node]
        elif isinstance(value, Mapping):
            return self._walk_mapping(value, path)
        elif isinstance(value, (list, tuple, set, frozenset)):
            return self._walk_sequence(value, path)

        return value, []

    def _walk_mapping(self, value: Mapping, path: str) -> tuple[Any, list[Node]]:
        result: dict[Any, Any] = {}
        nodes: list[Node] = []
        for key, item in value.items():
            child_path = _join_path(path, key)
            new_key, key_nodes = self._walk(key, child_path)
            new_item, item_nodes = self._walk(item, child_path)
            result[new_key] = new_item
            nodes.extend(key_nodes)
            nodes.extend(item_nodes)

        return result, nodes

    def _walk_sequence(self, value: Sequence, path: str) -> tuple[Any, list[Node]]:
        result = []
        nodes: list[Node] = []
        for index, item in enumerate(value):
            child_path = _join_path(path, f"[{index}]")
            new_item, item_nodes = self._walk(item, child_path)
            result.append(new_item)
            nodes.extend(item_nodes)

        if isinstance(value, tuple):
            return tuple(result), nodes
        elif isinstance(value, frozenset):
            return frozenset(result), nodes
        elif isinstance(value, set):
            return set(result), nodes

        return result, nodes

    def _replay_tag(self, value: TaggedValue | CBORTag, path: str) -> tuple[Any, Node]:
        if isinstance(value, TaggedValue):
            tagged = value
        else:
            tagged = TaggedValue(value.tag, value.value)

        tag = tagged.tag
        new_rule = self.new_rules.get(tag)
        old_rule = self.old_rules.get(tag)
        change = self.diff.changes.get(tag)
        node = Node(
            tag=tag,
            state=ValueState.OPAQUE,
            rule=new_rule,
            previous_rule=old_rule,
            old_value=tagged.payload,
            new_value=tagged.payload,
            decoded=tagged.payload,
            raw=tagged.raw,
            tag_raw=_tag_bytes(tag),
            source=path or tagged.source,
            original=value,
        )

        if new_rule is None:
            if old_rule is not None and change is not None:
                node.state = ValueState.PENDING
                node.reason = (
                    f"tag {tag} is no longer interpreted under rules "
                    f"{self.new_rules.version}; the previous value is kept but its "
                    "former type is not reproduced"
                )
                return value, node

            node.state = ValueState.OPAQUE
            node.reason = f"no rule for tag {tag}"
            # An opaque outer tag grants no authority to nested tags: the
            # payload is left exactly as recorded, tag included.
            return value, node

        if new_rule.receives_raw:
            if tagged.raw is None:
                node.state = ValueState.PENDING
                node.reason = (
                    f"tag {tag} requires raw payload bytes for semantic "
                    f"{new_rule.semantic!r}, but they are not available"
                )
                return value, node

            # A raw handler sees the original bytes.  Nested tags inside the
            # payload cannot be substituted for it, but in-scope nested tags
            # still have to be inspected so dependency propagation notices
            # when the outer result must be produced again.
            handler_payload = tagged.raw
            nested_nodes = self._inspect_raw_payload(tagged.payload, new_rule, path)
        else:
            payload = tagged.payload
            if payload is None:
                if tagged.raw is not None and self.decode is not None:
                    try:
                        payload = self.decode(tagged.raw, tag)
                    except Exception as exc:
                        node.state = ValueState.PENDING
                        node.reason = f"could not decode payload for tag {tag}: {exc}"
                        return value, node
                else:
                    node.state = ValueState.PENDING
                    node.reason = (
                        f"tag {tag} requires an interpreted payload but none is available"
                    )
                    return value, node
            elif (
                isinstance(payload, (bytes, bytearray, memoryview))
                and tagged.raw is not None
                and self.decode is not None
            ):
                payload = self._maybe_decode(payload, tagged.raw, tag, node)
                if node.state is ValueState.PENDING:
                    return value, node

            handler_payload, nested_nodes = self._replay_payload(payload, new_rule, path)

        node.inputs = list(nested_nodes)

        child_pending = next((child for child in nested_nodes if child.pending), None)
        if child_pending is not None and not new_rule.receives_raw:
            node.state = ValueState.PENDING
            node.reason = f"nested tag {child_pending.tag} at {child_pending.source!r} is pending"
            return value, node

        # A raw handler sees the original bytes.  A different nested
        # interpretation does not alter those bytes, so it cannot change the
        # outer result.  Only interpreted handlers consume the replayed value.
        child_changed = not new_rule.receives_raw and any(child.affected for child in nested_nodes)
        if change is None or not change.affects_interpretation:
            if not child_changed:
                # Nothing in this tag's rule or in its declared dependencies
                # changed, so the recorded value stands.
                return value, node

        handler = self.handlers.get(new_rule.semantic)
        if handler is None:
            node.state = ValueState.PENDING
            node.reason = f"no handler registered for semantic {new_rule.semantic!r}"
            return value, node

        node.replayed = True
        context = ReplayContext(self, new_rule, node, handler_payload, self.new_rules.version)
        try:
            result = handler(context)
        except CBORReplayError as exc:
            node.replayed = False
            node.state = ValueState.PENDING
            node.reason = str(exc)
            return value, node
        except Exception as exc:
            node.replayed = False
            node.state = ValueState.PENDING
            node.reason = f"handler {new_rule.semantic!r} failed: {exc}"
            return value, node

        if isinstance(result, _PendingPayload):
            node.replayed = False
            node.state = ValueState.PENDING
            node.reason = result.reason
            return value, node

        node.state = ValueState.INTERPRETED
        if _is_tag(result):
            if result.tag != tag:
                node.retagged = True
                node.reason = f"tag {tag} was retagged as {result.tag}"
                node.retagged_tag = result.tag
                node.rule = self.new_rules.get(result.tag)
                node.new_value = result.value
                return node.value, node

            result = result.value

        node.new_value = result
        return node.value, node

    def _maybe_decode(self, payload: Any, raw: bytes | None, tag: int, node: Node) -> Any:
        """Decode raw bytes when a value handed to an interpreted handler is still raw."""

        if raw is not None and self.decode is not None and not _looks_decoded(payload):
            try:
                return self.decode(raw, tag)
            except Exception as exc:
                node.state = ValueState.PENDING
                node.reason = f"could not decode payload for tag {tag}: {exc}"

        return payload

    def _in_scope(self, rule: TagRule, tag: int) -> bool:
        return rule.dependencies == "all" or tag in rule.dependencies

    def _inspect_raw_payload(self, payload: Any, rule: TagRule, path: str) -> list[Node]:
        """Inspect nested tags without replacing the raw handler's payload."""

        nodes: list[Node] = []
        if _is_tag(payload):
            tag = payload.tag
            if not self._in_scope(rule, tag):
                # Out of the declared dependency scope.  The outer tag does
                # not authorise the inner vendor handler to run.
                return nodes

            new_rule = self.new_rules.get(tag)
            old_rule = self.old_rules.get(tag)
            if new_rule is None:
                payload_value = (
                    payload.payload if isinstance(payload, TaggedValue) else payload.value
                )
                node = Node(
                    tag=tag,
                    state=ValueState.OPAQUE,
                    previous_rule=old_rule,
                    old_value=payload_value,
                    new_value=payload_value,
                    source=path,
                    original=payload,
                )
                nodes.append(node)
                return nodes

            _, node = self._replay_tag(payload, path)
            nodes.append(node)
            return nodes

        if isinstance(payload, Mapping):
            for key, item in payload.items():
                nodes.extend(self._inspect_raw_payload(key, rule, path))
                nodes.extend(self._inspect_raw_payload(item, rule, path))
        elif isinstance(payload, (list, tuple, set, frozenset)):
            for item in payload:
                nodes.extend(self._inspect_raw_payload(item, rule, path))

        return nodes

    def _replay_payload(self, payload: Any, rule: TagRule, path: str) -> tuple[Any, list[Node]]:
        """Replay nested tags permitted by *rule*'s dependency scope."""

        nodes: list[Node] = []
        if _is_tag(payload):
            tag = payload.tag
            if not self._in_scope(rule, tag):
                # Unknown or out-of-scope nested tag: keep it opaque.  In
                # particular, an outer tag does not authorise a vendor's inner
                # handler just because it was able to decode the structure.
                return payload, nodes

            new_value, node = self._replay_tag(payload, path)
            nodes.append(node)
            if node.state is ValueState.INTERPRETED:
                return new_value, nodes
            elif node.state is ValueState.OPAQUE and node.rule is None:
                return payload, nodes

            # The nested tag keeps its recorded interpretation.  An outer
            # handler that is replayed for its own reason still receives the
            # prior decoded value, not the tag wrapper.
            prior = payload.prior_value if isinstance(payload, TaggedValue) else payload.value
            return prior, nodes

        if isinstance(payload, Mapping):
            result = {}
            for key, item in payload.items():
                new_key, key_nodes = self._replay_payload(key, rule, path)
                new_item, item_nodes = self._replay_payload(item, rule, path)
                result[new_key] = new_item
                nodes.extend(key_nodes)
                nodes.extend(item_nodes)

            return result, nodes

        if isinstance(payload, (list, tuple, set, frozenset)):
            result = []
            for item in payload:
                new_item, item_nodes = self._replay_payload(item, rule, path)
                result.append(new_item)
                nodes.extend(item_nodes)

            if isinstance(payload, tuple):
                return tuple(result), nodes
            elif isinstance(payload, frozenset):
                return frozenset(result), nodes
            elif isinstance(payload, set):
                return set(result), nodes

            return result, nodes

        return payload, nodes

    def _materialise(self, value: Any) -> Any:
        if isinstance(value, TaggedValue):
            return value.payload
        elif _is_tag(value):
            return value.value
        elif isinstance(value, Mapping):
            return {key: self._materialise(item) for key, item in value.items()}
        elif isinstance(value, (list, tuple, set, frozenset)):
            return type(value)(self._materialise(item) for item in value)

        return value


def _looks_decoded(payload: Any) -> bool:
    """Whether *payload* already looks like decoded CBOR rather than raw bytes."""

    return not isinstance(payload, (bytes, bytearray, memoryview))


def _change_reason(change: RuleChange) -> str:
    if change.kind == "added":
        return "rule added"
    elif change.kind == "removed":
        return "rule removed"

    return "rule changed: " + ", ".join(change.behavioural_attributes)


class Ticket:
    """A pending field that can be supplied with a complete interpretation."""

    __slots__ = ("result", "path", "tag", "raw", "rule", "source")

    def __init__(self, result: FieldResult, node: Node) -> None:
        self.result = result
        self.path = result.path
        self.tag = node.tag
        self.raw = node.raw
        self.rule = node.rule
        self.source = node.source

    def __repr__(self) -> str:
        return f"Ticket({self.path!r}, tag={self.tag})"


def pending_tickets(result: RecordResult) -> list[Ticket]:
    """Return one ticket per pending node of *result*.

    A nested preamble node gets its own ticket even when the outer field is
    pending because of it, so the missing interpretation path stays visible.
    """

    tickets: list[Ticket] = []
    seen: set[tuple[str, int, str]] = set()
    for field_result in result.pending:
        pending_nodes = [node for node in field_result.nodes if node.pending] or [
            Node(tag=field_result.old_tag or 0, state=ValueState.PENDING)
        ]
        for node in pending_nodes:
            key = (field_result.path, node.tag, node.reason or "")
            if key in seen:
                continue

            seen.add(key)
            node_result = field_result
            if node.source and node.source != field_result.path:
                node_result = FieldResult(
                    path=node.source,
                    status="pending",
                    old_value=node.old_value,
                    new_value=node.value,
                    old_tag=node.tag,
                    new_tag=node.new_tag,
                    old_rule=node.previous_rule,
                    new_rule=node.rule,
                    reason=node.reason,
                    nodes=[node],
                )

            tickets.append(Ticket(node_result, node))

    return tickets


def replay_records(
    records: Mapping[str, Any],
    old_rules: RuleSet,
    new_rules: RuleSet,
    handlers: Mapping[str, Handler] | None = None,
    decode: Callable[[bytes, int], Any] | None = None,
) -> dict[str, RecordResult]:
    """Replay several named records with one engine.

    :param records: mapping of field name to record
    :param old_rules: the rules used for the recorded values
    :param new_rules: the rules that must now be applied
    :param handlers: semantic handlers
    :param decode: optional raw payload decoder
    :return: mapping of field name to its :class:`RecordResult`
    """

    replay = Replay(old_rules, new_rules, handlers, decode)
    return {name: replay.replay(record, source=name) for name, record in records.items()}
