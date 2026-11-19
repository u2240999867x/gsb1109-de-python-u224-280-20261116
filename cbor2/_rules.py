"""Versioned CBOR tag interpretation rules.

A :class:`RuleSet` describes how a single version of a CBOR consumer
interprets semantic tags.  Rules are deliberately declarative: they state
which semantic handler is used, whether the handler receives raw payload
bytes or the already interpreted payload, and which nested tags the handler
is allowed to depend on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, overload

from ._types import CBORError

DependencyScope = frozenset[int] | Literal["all"]
InputKind = Literal["raw", "interpreted"]

_BEHAVIOURAL_FIELDS = ("semantic", "input", "dependencies")


class CBORRuleError(CBORError, ValueError):
    """Raised when a tag rule or rule set is malformed."""


def _validate_tag(tag: Any, kind: str = "tag") -> int:
    if isinstance(tag, bool) or not isinstance(tag, int):
        raise CBORRuleError(f"{kind} must be an integer")

    if not 0 <= tag < 2**64:
        raise CBORRuleError(f"{kind} must be a positive integer less than 2**64")

    return tag


def _normalise_dependencies(dependencies: Any) -> DependencyScope:
    if isinstance(dependencies, str):
        if dependencies == "all":
            return "all"

        raise CBORRuleError("dependencies must be 'all' or a collection of tag numbers")

    if dependencies is None:
        return frozenset()

    if isinstance(dependencies, int) and not isinstance(dependencies, bool):
        dependencies = (dependencies,)

    try:
        tags = frozenset(_validate_tag(tag, "dependency tag") for tag in dependencies)
    except TypeError as exc:
        raise CBORRuleError("dependencies must be 'all' or a collection of tag numbers") from exc

    return tags


@dataclass(frozen=True)
class TagRule:
    """How one tag is interpreted by one version of the rules.

    :param tag: the CBOR tag number
    :param semantic: name of the handler, e.g. ``"datetime-string"``
    :param input: ``"raw"`` when the handler receives the original payload
        bytes, or ``"interpreted"`` when it receives the interpreted payload
    :param dependencies: nested tag numbers whose interpretation the handler's
        result depends on, or ``"all"``.  For interpreted handlers this is
        also the scope of nested tags that may be interpreted before the
        handler is called.  An empty scope means that nested vendor tags stay
        opaque.
    :param description: optional human readable note; changing only the
        description does not require a field to be replayed
    """

    tag: int
    semantic: str
    input: InputKind = "interpreted"
    dependencies: DependencyScope = frozenset()
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag", _validate_tag(self.tag))
        if not isinstance(self.semantic, str) or not self.semantic:
            raise CBORRuleError("semantic must be a non-empty string")

        if self.input not in ("raw", "interpreted"):
            raise CBORRuleError("input must be either 'raw' or 'interpreted'")

        object.__setattr__(self, "dependencies", _normalise_dependencies(self.dependencies))
        if not isinstance(self.description, str):
            raise CBORRuleError("description must be a string")

    @property
    def receives_raw(self) -> bool:
        """Whether the semantic handler receives the original payload bytes."""

        return self.input == "raw"

    @property
    def dependency_tags(self) -> DependencyScope:
        """Alias for :attr:`dependencies`."""

        return self.dependencies

    def depends_on(self, tag: int) -> bool:
        """Return whether this rule declares a dependency on *tag*."""

        if self.dependencies == "all":
            return True

        return tag in self.dependencies

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON compatible representation of the rule."""

        dependencies: list[int] | str
        if self.dependencies == "all":
            dependencies = "all"
        else:
            dependencies = sorted(self.dependencies)

        return {
            "tag": self.tag,
            "semantic": self.semantic,
            "input": self.input,
            "dependencies": dependencies,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], tag: int | None = None) -> TagRule:
        """Build a rule from a mapping.

        The common alternative spellings ``depends_on``/``dependency_scope``
        for ``dependencies`` and ``handler``/``name`` for ``semantic`` are
        accepted to make hand written rule files less brittle.
        """

        if not isinstance(data, Mapping):
            raise CBORRuleError("a tag rule must be a mapping")

        if tag is None:
            if "tag" not in data:
                raise CBORRuleError("tag rule is missing its 'tag' field")

            tag = data["tag"]

        semantic = data.get("semantic", data.get("handler", data.get("name")))
        if semantic is None:
            raise CBORRuleError("tag rule is missing its 'semantic' field")

        dependencies = data.get(
            "dependencies", data.get("depends_on", data.get("dependency_scope", ()))
        )
        input_kind = data.get("input", data.get("receives", "interpreted"))
        description = data.get("description", data.get("note", ""))
        return cls(
            tag=_validate_tag(tag),
            semantic=semantic,
            input=input_kind,
            dependencies=dependencies,
            description=description,
        )


@dataclass(frozen=True)
class RuleChange:
    """The difference for one tag between two rule versions."""

    tag: int
    kind: Literal["added", "removed", "modified"]
    old: TagRule | None
    new: TagRule | None
    attributes: tuple[str, ...] = ()

    @property
    def affects_interpretation(self) -> bool:
        """Whether this change can alter the interpretation of a field."""

        if self.kind != "modified":
            return True

        return any(attribute in _BEHAVIOURAL_FIELDS for attribute in self.attributes)

    @property
    def behavioural_attributes(self) -> tuple[str, ...]:
        """The changed attributes that affect interpretation."""

        return tuple(
            attribute for attribute in self.attributes if attribute in _BEHAVIOURAL_FIELDS
        )


@dataclass(frozen=True)
class RuleSetDiff:
    """The result of comparing two :class:`RuleSet` instances."""

    old_version: str
    new_version: str
    changes: Mapping[int, RuleChange]

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))

    @property
    def changed_tags(self) -> frozenset[int]:
        """All tags with a change, including description-only changes."""

        return frozenset(self.changes)

    @property
    def behavioural_tags(self) -> frozenset[int]:
        """Tags whose interpretation may change."""

        return frozenset(
            tag for tag, change in self.changes.items() if change.affects_interpretation
        )

    def affects_tag(self, tag: int) -> bool:
        """Return whether *tag* needs to be interpreted again."""

        change = self.changes.get(tag)
        return change is not None and change.affects_interpretation

    def __bool__(self) -> bool:
        return bool(self.changes)

    def __contains__(self, tag: object) -> bool:
        return tag in self.changes


@dataclass(frozen=True)
class RuleSet:
    """A named set of tag interpretation rules."""

    version: str
    rules: Mapping[int, TagRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise CBORRuleError("version must be a non-empty string")

        normalised: dict[int, TagRule] = {}
        for key, rule in self.rules.items():
            if not isinstance(rule, TagRule):
                raise CBORRuleError("rules must map tag numbers to TagRule instances")

            if rule.tag != key:
                raise CBORRuleError(
                    f"rule for tag {key} declares a different tag number ({rule.tag})"
                )

            normalised[_validate_tag(key)] = rule

        object.__setattr__(self, "rules", MappingProxyType(normalised))

    @overload
    def __getitem__(self, tag: int) -> TagRule: ...

    @overload
    def __getitem__(self, tag: object) -> TagRule: ...

    def __getitem__(self, tag: int) -> TagRule:
        return self.rules[tag]

    def __contains__(self, tag: object) -> bool:
        return tag in self.rules

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def get(self, tag: int, default: Any = None) -> TagRule | Any:
        """Return the rule for *tag* or *default*."""

        return self.rules.get(tag, default)

    @property
    def tags(self) -> frozenset[int]:
        """The tag numbers covered by this rule set."""

        return frozenset(self.rules)

    def diff(self, other: RuleSet) -> RuleSetDiff:
        """Return the changes needed to go from this rule set to *other*."""

        if not isinstance(other, RuleSet):
            raise TypeError("other must be a RuleSet")

        changes: dict[int, RuleChange] = {}
        for tag in self.tags | other.tags:
            old = self.rules.get(tag)
            new = other.rules.get(tag)
            if old is None and new is not None:
                changes[tag] = RuleChange(tag, "added", old, new)
            elif old is not None and new is None:
                changes[tag] = RuleChange(tag, "removed", old, new)
            elif old != new:
                attributes = tuple(
                    name
                    for name in ("semantic", "input", "dependencies", "description")
                    if getattr(old, name) != getattr(new, name)
                )
                changes[tag] = RuleChange(tag, "modified", old, new, attributes)

        return RuleSetDiff(self.version, other.version, changes)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuleSet:
        """Build a rule set from a JSON compatible mapping.

        Accepted layouts::

            {"version": "v2", "rules": [{"tag": 0, "semantic": "..."}]}
            {"version": "v2", "rules": {"0": {...}}}
            {"version": "v2", "0": {...}, "1": {...}}
        """

        if not isinstance(data, Mapping):
            raise CBORRuleError("a rule set must be a mapping")

        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise CBORRuleError("rule set is missing its 'version' field")

        raw_rules = data.get("rules")
        rules: dict[int, TagRule] = {}
        if raw_rules is None:
            raw_rules = {key: value for key, value in data.items() if key != "version"}

        if isinstance(raw_rules, Mapping):
            iterable = raw_rules.items()
            for key, value in iterable:
                try:
                    tag = int(key)
                except (TypeError, ValueError) as exc:
                    raise CBORRuleError(f"invalid tag number {key!r}") from exc

                rule = value if isinstance(value, TagRule) else TagRule.from_dict(value, tag)
                if rule.tag != tag:
                    raise CBORRuleError(
                        f"rule for tag {tag} declares a different tag number ({rule.tag})"
                    )

                rules[tag] = rule
        else:
            try:
                iterable = list(raw_rules)
            except TypeError as exc:
                raise CBORRuleError("'rules' must be a mapping or a sequence") from exc

            for item in iterable:
                rule = item if isinstance(item, TagRule) else TagRule.from_dict(item)
                if rule.tag in rules:
                    raise CBORRuleError(f"duplicate rule for tag {rule.tag}")

                rules[rule.tag] = rule

        return cls(version, rules)


def builtin_rules(version: str = "builtin") -> RuleSet:
    """Return rules matching cbor2's standard semantic tag support.

    The returned rule set is a useful starting point for overriding only the
    tags whose interpretation changed.  Stateful extension tags such as string
    references are intentionally not part of this rule set.
    """

    rules = [
        TagRule(0, "datetime-string", "raw"),
        TagRule(1, "epoch-datetime", "raw"),
        TagRule(2, "positive-bignum", "raw"),
        TagRule(3, "negative-bignum", "raw"),
        TagRule(4, "decimal-fraction", "interpreted", frozenset({2, 3})),
        TagRule(5, "bigfloat", "interpreted", frozenset({2, 3})),
        TagRule(24, "embedded-cbor", "raw", "all"),
        TagRule(30, "rational", "interpreted", frozenset({2, 3})),
        TagRule(35, "regexp", "raw"),
        TagRule(36, "mime", "raw"),
        TagRule(37, "uuid", "raw"),
        TagRule(100, "epoch-date", "raw"),
        TagRule(258, "set", "interpreted", "all"),
        TagRule(260, "ipaddress", "raw"),
        TagRule(261, "ipnetwork", "interpreted", "all"),
        TagRule(1004, "date-string", "raw"),
        TagRule(55799, "self-describe", "interpreted", "all"),
    ]
    return RuleSet(version, {rule.tag: rule for rule in rules})
