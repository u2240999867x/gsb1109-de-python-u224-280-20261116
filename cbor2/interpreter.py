"""
Deferred interpretation of instrument labels backed by external dictionaries.

A labeled object is a CBOR semantic tag whose tag number matches a
:class:`LabelRule`.  The rule names the external dictionary (and its minimum
version) needed to interpret the label carried inside the tag payload.

The interpretation layer sits on top of ordinary CBOR decoding:

* objects whose dictionary (or dictionary version) is not available yet are
  kept as :class:`PendingObject` handles instead of being half interpreted;
* once the missing dictionaries have been supplied, :meth:`InterpretationSession.process`
  publishes queryable results;
* every shared object (CBOR tags 28/29) is interpreted exactly once and every
  reference keeps pointing at that single interpretation;
* failures are isolated per object (or per batch for corrupt encodings) so that
  unrelated top-level objects can still be published;
* interpretation order is derived from a dependency graph, never from
  traversal luck -- a labeled dependency cycle without a legal ordering is
  reported through :class:`CyclicInterpretationError`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._decoder import loads
from ._types import CBORDecodeError, CBORError, CBORTag

#: Sentinel used internally while materializing a result view.
_MISSING = object()


class InterpretationError(CBORError):
    """Base class for errors raised by the interpretation layer."""


class MissingDictionaryError(InterpretationError):
    """
    Raised (in a non-terminal way) when the dictionary or dictionary version
    required to interpret a label has not been supplied yet, or when the
    currently supplied dictionary does not yet contain the label.

    The affected object stays pending and is retried once a suitable
    dictionary (or a newer dictionary version defining the label) becomes
    available.
    """

    def __init__(
        self,
        dictionary_id: Hashable,
        required_version: int,
        label: Any = None,
    ):
        self.dictionary_id = dictionary_id
        self.required_version = required_version
        self.label = label
        if label is None:
            message = (
                f"dictionary {dictionary_id!r} version >= {required_version} is not available"
            )
        else:
            message = (
                f"label {label!r} is not yet defined by dictionary "
                f"{dictionary_id!r} (need version >= {required_version})"
            )

        super().__init__(message)


class LabelRuleRejected(InterpretationError):
    """
    Raised when a label rule refuses an object (a malformed label payload, for
    example a missing label key, or a failing validator).

    This is a terminal failure: retries never re-interpret the object.  A label
    that simply is not present in the supplied dictionary is reported through
    :class:`MissingDictionaryError` instead, because a later dictionary version
    may still define it.
    """


class CorruptEncodingError(InterpretationError):
    """
    Raised when the raw CBOR encoding of a batch is damaged and cannot be
    decoded at all.

    This is a terminal failure for the affected batch; other batches remain
    unaffected.
    """


class CyclicInterpretationError(InterpretationError):
    """
    Raised when labeled objects depend on each other in a way that leaves no
    legal interpretation ordering.

    :param blocking_references:
        sorted ``(from_object_id, to_object_id)`` edges that make up the
        blocking closure, so the obstruction can be reported explicitly
        instead of being decided by traversal order.
    """

    def __init__(self, blocking_references: Sequence[tuple[int, int]]):
        self.blocking_references = tuple(blocking_references)
        edges = ", ".join(f"#{source} -> #{target}" for source, target in self.blocking_references)
        super().__init__(f"no legal interpretation ordering; blocking references: {edges}")


class PendingStatus(Enum):
    """Lifecycle states of a :class:`PendingObject`."""

    #: waiting for a dictionary or for dependencies to be published
    pending = "pending"
    #: interpretation confirmed and published
    published = "published"
    #: interpretation terminally rejected
    failed = "failed"


@dataclass(frozen=True)
class LabelRule:
    """
    Declaration of how tagged objects carrying instrument labels are interpreted.

    :param tag:
        CBOR semantic tag number the rule applies to.
    :param dictionary_id:
        identifier of the external dictionary that provides the label entries.
    :param label_key:
        key under which the label code is expected in the tag payload map.
    :param required_version:
        minimum dictionary version that must be available before interpretation
        is attempted.
    :param validator:
        optional callback receiving the raw tag payload.  Any raised exception
        is converted into a :class:`LabelRuleRejected` failure.
    """

    tag: int
    dictionary_id: Hashable
    label_key: str = "label"
    required_version: int = 1
    validator: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class LabelDictionary:
    """
    A versioned external dictionary mapping label codes to their meanings.

    Only the highest version supplied for a given ``dictionary_id`` is retained.
    """

    dictionary_id: Hashable
    version: int
    entries: Mapping[Any, Any]


@dataclass(eq=False)
class InterpretedObject:
    """
    The single published interpretation of a labeled object.

    The same instance is returned for every reference to the underlying shared
    CBOR object.  Equality is identity based so the object stays hashable even
    when its payload contains a reference back to itself.
    """

    label: Any
    name: Any
    payload: Any = None


class PendingObject:
    """
    Handle for a labeled object whose interpretation is not yet confirmed.

    Instances are never half-built interpretations: they only describe the
    object, its state and (once available) its terminal error.
    """

    __slots__ = (
        "id",
        "key",
        "rule",
        "tag",
        "label",
        "top_indexes",
        "dependencies",
        "status",
        "error",
        "interpretation",
        "_attempts",
    )

    def __init__(
        self,
        object_id: int,
        key: tuple[int, int],
        rule: LabelRule,
        tag: CBORTag,
        label: Any,
        top_indexes: frozenset[tuple[int, int]],
    ) -> None:
        self.id = object_id
        self.key = key
        self.rule = rule
        self.tag = tag
        self.label = label
        self.top_indexes = top_indexes
        self.dependencies: frozenset[PendingObject] = frozenset()
        self.status = PendingStatus.pending
        self.error: InterpretationError | None = None
        self.interpretation: InterpretedObject | None = None
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Number of interpretation attempts made (confirmed work is not retried)."""
        return self._attempts

    def mark_attempt(self) -> None:
        self._attempts += 1

    def publish(self, interpretation: InterpretedObject) -> None:
        self.interpretation = interpretation
        self.error = None
        self.status = PendingStatus.published

    def fail(self, error: InterpretationError) -> None:
        self.error = error
        self.status = PendingStatus.failed

    def __repr__(self) -> str:
        return f"PendingObject(id={self.id}, tag={self.rule.tag}, label={self.label!r}, status={self.status.name})"


@dataclass(frozen=True)
class InterpretationFailure:
    """A terminal failure attached to a batch."""

    batch_id: int
    error: InterpretationError
    item_index: int | None = None
    object_id: int | None = None


class _NotPublished(Exception):
    """Internal signal that a labeled tag has no published interpretation yet."""

    def __init__(self, pending: PendingObject) -> None:
        self.pending = pending
        super().__init__(f"object #{pending.id} is {pending.status.name}")


@dataclass
class _BatchRecord:
    batch_id: int
    raw: bytes
    items: list[Any] = field(default_factory=list)
    decode_error: CorruptEncodingError | None = None
    object_keys: set[tuple[int, int]] = field(default_factory=set)


class BatchOutcome:
    """
    Snapshot of a batch after a :meth:`InterpretationSession.process` run.

    Only fully interpreted top-level objects are exposed as normal values in
    :meth:`successful`; blocked or failed objects are reported separately so a
    half-finished branch can never masquerade as a result.
    """

    def __init__(
        self,
        session: InterpretationSession,
        batch: _BatchRecord,
        published_views: Mapping[int, Any],
    ) -> None:
        self._session = session
        self._batch = batch
        self._views = dict(published_views)

    @property
    def batch_id(self) -> int:
        return self._batch.batch_id

    def _batch_objects(self) -> tuple[PendingObject, ...]:
        return tuple(
            session_object
            for session_object in self._session.objects
            if session_object.key in self._batch.object_keys
        )

    @property
    def is_complete(self) -> bool:
        """``True`` when the batch decoded and every object was published."""
        if self._batch.decode_error is not None:
            return False

        return all(
            session_object.status is PendingStatus.published
            for session_object in self._batch_objects()
        )

    def successful(self) -> dict[int, Any]:
        """Return a copy of ``{item_index: published value}``."""
        return dict(self._views)

    def pending(self) -> tuple[PendingObject, ...]:
        """Return the objects still waiting for a dictionary."""
        return tuple(
            session_object
            for session_object in self._batch_objects()
            if session_object.status is PendingStatus.pending
        )

    def failures(self) -> tuple[InterpretationFailure, ...]:
        """Return all terminal failures touching this batch."""
        failures: list[InterpretationFailure] = []
        if self._batch.decode_error is not None:
            failures.append(InterpretationFailure(self._batch.batch_id, self._batch.decode_error))

        for session_object in self._batch_objects():
            if session_object.status is PendingStatus.failed and session_object.error is not None:
                item_index = next(
                    index
                    for batch_id, index in session_object.top_indexes
                    if batch_id == self._batch.batch_id
                )
                failures.append(
                    InterpretationFailure(
                        self._batch.batch_id,
                        session_object.error,
                        item_index=item_index,
                        object_id=session_object.id,
                    )
                )

        return tuple(failures)

    def is_published(self, item_index: int) -> bool:
        return item_index in self._views

    def get(self, item_index: int) -> Any:
        """
        Return the published value for a top-level item.

        :raises CorruptEncodingError: if the batch itself could not be decoded
        :raises InterpretationError: if the item is blocked by a pending or
            failed labeled object
        """
        if self._batch.decode_error is not None:
            raise self._batch.decode_error
        if item_index in self._views:
            return self._views[item_index]

        location = (self._batch.batch_id, item_index)
        for session_object in self._batch_objects():
            if location not in session_object.top_indexes:
                continue

            if session_object.status is PendingStatus.failed and session_object.error:
                raise session_object.error
            if session_object.status is PendingStatus.pending:
                if session_object.error is not None:
                    raise session_object.error
                rule = session_object.rule
                raise MissingDictionaryError(rule.dictionary_id, rule.required_version)

        raise KeyError(f"no item with index {item_index} in batch {self._batch.batch_id}")


class InterpretationSession:
    """
    Holds the isolated state for one or more batches sharing the same rules and
    dictionaries.

    Two batches that need different dictionaries should use two sessions:
    pending objects, confirmed interpretations and supplied dictionaries never
    leak between sessions.
    """

    def __init__(
        self,
        rules: Sequence[LabelRule],
        dictionaries: Sequence[LabelDictionary] = (),
    ) -> None:
        self._rules: dict[int, LabelRule] = {rule.tag: rule for rule in rules}
        self._dictionaries: dict[Hashable, LabelDictionary] = {}
        for dictionary in dictionaries:
            self.add_dictionary(dictionary)

        self._objects: list[PendingObject] = []
        # A logical labeled object is identified by (tag number, payload id):
        # the encoder puts the share marker on the payload container, so the
        # CBORTag wrapper is rebuilt at every reference while the payload stays
        # one and the same object.
        self._registry: dict[tuple[int, int], PendingObject] = {}
        self._batches: list[_BatchRecord] = []
        self._label_memo: dict[tuple[int, int], InterpretedObject] = {}
        self._node_memo: dict[int, Any] = {}

    @property
    def objects(self) -> tuple[PendingObject, ...]:
        """All labeled objects known to this session."""
        return tuple(self._objects)

    @property
    def pending_objects(self) -> tuple[PendingObject, ...]:
        """Objects still in the :attr:`PendingStatus.pending` state."""
        return tuple(
            session_object
            for session_object in self._objects
            if session_object.status is PendingStatus.pending
        )

    def add_dictionary(self, dictionary: LabelDictionary) -> None:
        """
        Supply (or upgrade) an external dictionary.

        Older or equally versioned dictionaries for the same id are ignored,
        and dictionaries belonging to another session never interact.
        """
        existing = self._dictionaries.get(dictionary.dictionary_id)
        if existing is None or dictionary.version > existing.version:
            self._dictionaries[dictionary.dictionary_id] = dictionary

    def add_batch(self, data: bytes) -> int:
        """
        Decode a batch (a CBOR array of top-level objects) and register every
        labeled object found in it.

        Corrupt raw encoding is captured as a :class:`CorruptEncodingError` on
        the batch instead of tearing down the whole session.

        :returns: the id assigned to the batch
        """
        batch = _BatchRecord(batch_id=len(self._batches), raw=bytes(data))
        self._batches.append(batch)
        try:
            decoded = loads(data)
        except CBORDecodeError as exc:
            batch.decode_error = CorruptEncodingError(
                f"batch {batch.batch_id} has a corrupt encoding: {exc}"
            )
            return batch.batch_id

        batch.items = list(decoded) if isinstance(decoded, list) else [decoded]

        for item_index, item in enumerate(batch.items):
            location = (batch.batch_id, item_index)
            for tag in self._iter_labeled_tags(item):
                key = self._tag_key(tag)
                batch.object_keys.add(key)
                pending = self._registry.get(key)
                if pending is None:
                    rule = self._rules[tag.tag]
                    pending = PendingObject(
                        object_id=len(self._objects),
                        key=key,
                        rule=rule,
                        tag=tag,
                        label=self._extract_label(rule, tag.value),
                        top_indexes=frozenset({location}),
                    )
                    self._objects.append(pending)
                    self._registry[key] = pending
                else:
                    pending.top_indexes = pending.top_indexes | frozenset({location})

        for pending in self._objects:
            if not pending.dependencies:
                pending.dependencies = frozenset(self._collect_dependencies(pending))

        return batch.batch_id

    def process(self) -> list[BatchOutcome]:
        """
        Run interpretation rounds and return a fresh outcome snapshot per batch.

        Confirmed interpretations and terminal failures are not re-executed;
        only objects still pending are reconsidered.  May be called again after
        supplying additional dictionaries.
        """
        # The structural memo only backs view construction for this run.  A
        # container cached while a nested label was still blocked would
        # otherwise be reused half-built after the dictionary arrives.
        self._node_memo = {}
        self._interpret_rounds()
        self._mark_cyclic_closures()
        self._propagate_failures()
        return [self._build_outcome(batch) for batch in self._batches]

    # -- registration helpers -------------------------------------------------

    def _tag_key(self, tag: CBORTag) -> tuple[int, int]:
        return tag.tag, id(tag.value)

    def _iter_labeled_tags(self, root: Any) -> list[CBORTag]:
        results: list[CBORTag] = []
        seen: set[int] = set()
        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            node_id = id(node)
            if node_id in seen:
                continue
            seen.add(node_id)
            if isinstance(node, CBORTag):
                if node.tag in self._rules:
                    results.append(node)
                stack.append(node.value)
            elif isinstance(node, Mapping):
                stack.extend(node.keys())
                stack.extend(node.values())
            elif isinstance(node, (list, tuple, set, frozenset)):
                stack.extend(node)

        return results

    @staticmethod
    def _extract_label(rule: LabelRule, payload: Any) -> Any:
        if isinstance(payload, Mapping) and rule.label_key in payload:
            return payload[rule.label_key]

        return None

    def _collect_dependencies(self, owner: PendingObject) -> set[PendingObject]:
        # Only *other* labeled objects reachable from the payload are ordering
        # dependencies.  A reference back into the owner's own payload (a
        # structural cycle) is legal and resolved by identity at materialization
        # time, so it must not turn into a self dependency.
        dependencies: set[PendingObject] = set()
        seen: set[int] = {id(owner.tag.value)}
        stack: list[Any] = [owner.tag.value]
        while stack:
            node = stack.pop()
            if isinstance(node, CBORTag):
                pending = self._registry.get(self._tag_key(node))
                if pending is not None and pending is not owner:
                    dependencies.add(pending)
                children = (node.value,)
            elif isinstance(node, Mapping):
                children = (*node.keys(), *node.values())
            elif isinstance(node, (list, tuple, set, frozenset)):
                children = tuple(node)
            else:
                continue

            for child in children:
                if id(child) not in seen:
                    seen.add(id(child))
                    stack.append(child)

        return dependencies

    # -- interpretation --------------------------------------------------------

    def _interpret_rounds(self) -> None:
        while True:
            ready = [
                pending
                for pending in self._objects
                if pending.status is PendingStatus.pending
                and all(
                    dependency.status is PendingStatus.published
                    for dependency in pending.dependencies
                )
            ]
            if not ready:
                break

            open_count = sum(pending.status is PendingStatus.pending for pending in self._objects)
            for pending in sorted(ready, key=lambda candidate: candidate.id):
                self._interpret_one(pending)

            self._propagate_failures()

            # Objects blocked on a missing dictionary stay "ready" but do not
            # change state; without this progress check the loop would spin.
            remaining = sum(pending.status is PendingStatus.pending for pending in self._objects)
            if remaining == open_count:
                break

    def _interpret_one(self, pending: PendingObject) -> None:
        pending.mark_attempt()
        rule = pending.rule
        dictionary = self._dictionaries.get(rule.dictionary_id)
        if dictionary is None or dictionary.version < rule.required_version:
            # Non-terminal: keep the object pending and remember the reason.
            pending.error = MissingDictionaryError(rule.dictionary_id, rule.required_version)
            return

        payload = pending.tag.value
        if not isinstance(payload, Mapping) or rule.label_key not in payload:
            pending.fail(
                LabelRuleRejected(
                    f"object #{pending.id} has no {rule.label_key!r} label in its payload"
                )
            )
            return

        label = pending.label
        name = dictionary.entries.get(label, _MISSING)
        if name is _MISSING:
            # The dictionary has arrived but this label is not defined in it
            # yet.  Treat this as "dictionary not complete" rather than a
            # rule refusal so a later dictionary version can fill the gap.
            pending.error = MissingDictionaryError(
                rule.dictionary_id, rule.required_version, label=label
            )
            return

        if rule.validator is not None:
            try:
                rule.validator(payload)
            except Exception as exc:
                pending.fail(LabelRuleRejected(f"validator rejected object #{pending.id}: {exc}"))
                return

        interpretation = InterpretedObject(label=label, name=name)
        # Publish before materializing the payload so structural cycles resolve
        # to this very instance.  Ordering dependencies are all published already.
        self._label_memo[pending.key] = interpretation
        try:
            interpretation.payload = self._materialize(pending.tag.value)
        except _NotPublished as exc:
            self._label_memo.pop(pending.key, None)
            pending.fail(
                InterpretationError(
                    f"object #{pending.id} references unpublished object #{exc.pending.id}"
                )
            )
            return

        pending.publish(interpretation)

    def _propagate_failures(self) -> None:
        changed = True
        while changed:
            changed = False
            for pending in self._objects:
                if pending.status is not PendingStatus.pending:
                    continue

                failed_dependency = next(
                    (
                        dependency
                        for dependency in pending.dependencies
                        if dependency.status is PendingStatus.failed
                    ),
                    None,
                )
                if failed_dependency is not None:
                    pending.fail(
                        InterpretationError(
                            f"object #{pending.id} depends on failed object "
                            f"#{failed_dependency.id}"
                        )
                    )
                    changed = True

    def _mark_cyclic_closures(self) -> None:
        remaining = {
            pending for pending in self._objects if pending.status is PendingStatus.pending
        }
        if not remaining:
            return

        on_cycle = {pending for pending in remaining if self._reaches(pending, pending, remaining)}
        if not on_cycle:
            return

        closure = set(on_cycle)
        reverse_edges: dict[int, set[PendingObject]] = {}
        for pending in remaining:
            for dependency in pending.dependencies:
                if dependency in remaining:
                    reverse_edges.setdefault(dependency.id, set()).add(pending)

        frontier = list(on_cycle)
        while frontier:
            node = frontier.pop()
            for ancestor in reverse_edges.get(node.id, set()):
                if ancestor not in closure:
                    closure.add(ancestor)
                    frontier.append(ancestor)

        blocking = sorted(
            (source.id, target.id)
            for source in closure
            for target in source.dependencies
            if target in closure
        )
        error = CyclicInterpretationError(blocking)
        for pending in closure:
            pending.fail(error)

    @staticmethod
    def _reaches(start: PendingObject, target: PendingObject, scope: set[PendingObject]) -> bool:
        seen: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for dependency in node.dependencies:
                if dependency not in scope:
                    continue
                if dependency is target:
                    return True
                if dependency.id not in seen:
                    seen.add(dependency.id)
                    stack.append(dependency)

        return False

    # -- result materialization ------------------------------------------------

    def _materialize(self, node: Any) -> Any:
        if isinstance(node, CBORTag):
            key = self._tag_key(node)
            pending = self._registry.get(key)
            if pending is not None:
                interpretation = self._label_memo.get(key)
                if interpretation is None:
                    raise _NotPublished(pending)
                return interpretation

            cached = self._node_memo.get(id(node), _MISSING)
            if cached is not _MISSING:
                return cached

            # Preserve the concrete tag type (Python or C implementation).
            result: Any = type(node)(node.tag, self._materialize(node.value))
            self._node_memo[id(node)] = result
            return result

        cached = self._node_memo.get(id(node), _MISSING)
        if cached is not _MISSING:
            return cached

        if isinstance(node, Mapping):
            result = {}
            self._node_memo[id(node)] = result
            for key, value in node.items():
                result[self._materialize(key)] = self._materialize(value)
            return result

        if isinstance(node, list):
            result = []
            self._node_memo[id(node)] = result
            result.extend(self._materialize(child) for child in node)
            return result

        if isinstance(node, tuple):
            return tuple(self._materialize(child) for child in node)

        if isinstance(node, (set, frozenset)):
            return frozenset(self._materialize(child) for child in node)

        return node

    def _build_outcome(self, batch: _BatchRecord) -> BatchOutcome:
        views: dict[int, Any] = {}
        if batch.decode_error is None:
            for item_index, item in enumerate(batch.items):
                try:
                    views[item_index] = self._materialize(item)
                except _NotPublished:
                    # The item is reported through pending()/failures() instead.
                    continue

        return BatchOutcome(self, batch, views)
