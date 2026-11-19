"""Version-aware MPD filter conditions.

MPD 0.21 added filter expressions to ``find`` and ``search``.  Older servers
accept only repeated ``TAG VALUE`` pairs: ``find`` compares exactly and case
sensitively, while ``search`` matches case insensitive substrings.  Repeating
pairs is an AND, and neither command can express OR or NOT.

The :class:`Filter` tree below keeps the caller's condition unchanged.  A
:class:`FilterPlan` translates it for the server version at hand.  When the
server language cannot express part of the condition, the plan asks for a
candidate superset and keeps the original condition as a local residual.
:func:`select_songs` then decides each returned record as confirmed, excluded,
or undetermined.  A record is undetermined when a field needed by the residual
is missing; such records are never silently excluded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

#: First MPD release that accepts filter expressions in find/search.
FILTER_EXPRESSION_VERSION = (0, 21, 0)

#: First MPD release with the starts_with filter operator.  Older releases
#: with filter expressions use contains as a candidate and verify locally.
STARTS_WITH_VERSION = (0, 22, 0)

#: Bound on expanded candidate terms before falling back to a full scan.
MAX_CANDIDATE_TERMS = 64

_COMPARISON_OPERATORS = ("==", "!=", "contains", "starts_with")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class FilterError(ValueError):
    """A filter expression is malformed or cannot be represented."""


class FilterNotSupported(FilterError):
    """The condition cannot be executed with server-side matching alone.

    ``plan`` carries the best available fallback: a candidate superset query
    plus the original condition as a local residual.
    """

    def __init__(self, message: str, plan: Optional["FilterPlan"] = None) -> None:
        super().__init__(message)
        self.plan = plan


def _validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        raise FilterError("invalid MPD tag name: {!r}".format(tag))
    return tag


def _validate_value(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("filter value must be a string")
    if "\n" in value or "\r" in value:
        raise FilterError("filter values cannot contain newlines")
    return value


def escape_value(value: str) -> str:
    """Escape a value for inclusion in an MPD filter expression.

    Backslashes are escaped first so a string that already ends in an escape
    cannot turn the closing quote into new syntax.
    """

    return value.replace("\\", "\\\\").replace('"', '\\"')


class _FilterMeta(type):
    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls is Filter and args:
            return _make_tag_filter(*args, **kwargs)
        return super().__call__(*args, **kwargs)


class Filter(metaclass=_FilterMeta):
    """Base class for immutable filter conditions.

    For convenience ``Filter(tag, value)`` and ``Filter(tag, operator, value)``
    construct a :class:`TagFilter`.  Subclasses are built with :class:`And`,
    :class:`Or` and :class:`Not`, or with the operators ``&``, ``|`` and ``~``.
    """

    def evaluate(self, song: Mapping[str, Any]) -> Optional[bool]:
        """Return True, False, or None when a needed field is unavailable."""

        raise NotImplementedError

    def to_mpd(self) -> str:
        """Render the condition as an MPD filter expression."""

        raise NotImplementedError

    def __and__(self, other: "Filter") -> "Filter":
        return And(self, other)

    def __or__(self, other: "Filter") -> "Filter":
        return Or(self, other)

    def __invert__(self) -> "Filter":
        return Not(self)

    @staticmethod
    def exact(tag: str, value: str, *, ignore_case: bool = False) -> "TagFilter":
        return TagFilter(tag, value, "==", ignore_case=ignore_case)

    @staticmethod
    def contains(tag: str, value: str, *, ignore_case: bool = True) -> "TagFilter":
        return TagFilter(tag, value, "contains", ignore_case=ignore_case)

    @staticmethod
    def starts_with(tag: str, value: str, *, ignore_case: bool = True) -> "TagFilter":
        return TagFilter(tag, value, "starts_with", ignore_case=ignore_case)

    @staticmethod
    def not_(condition: "Filter") -> "Filter":
        return Not(condition)

    @staticmethod
    def parse(text: str, *, ignore_case: bool = False) -> "Filter":
        return parse_filter(text, ignore_case=ignore_case)


def _make_tag_filter(*args: Any, **kwargs: Any) -> "TagFilter":
    if len(args) == 2:
        tag, value = args
        operator = kwargs.pop("op", "==")
    elif len(args) == 3:
        tag, operator, value = args
    else:
        raise TypeError("Filter() takes tag, value or tag, operator, value")
    ignore_case = kwargs.pop("ignore_case", operator in ("contains", "starts_with"))
    if kwargs:
        raise TypeError("unexpected keyword arguments: {}".format(", ".join(kwargs)))
    return TagFilter(str(tag), str(value), _normalize_operator(operator), ignore_case)


def _normalize_operator(operator: str) -> str:
    aliases = {
        "=": "==",
        "eq": "==",
        "==": "==",
        "!=": "!=",
        "ne": "!=",
        "contains": "contains",
        "starts_with": "starts_with",
        "startswith": "starts_with",
    }
    try:
        return aliases[operator.lower()]
    except (AttributeError, KeyError):
        raise FilterError("unsupported filter operator: {!r}".format(operator))


@dataclass(frozen=True)
class TagFilter(Filter):
    """A comparison against one tag.

    A song tag may hold several values.  Positive comparisons match when any
    value matches.  ``!=`` means "no value equals the given value", i.e. it is
    the negation of the positive comparison, not "some value differs".
    """

    tag: str
    value: str
    op: str = "=="
    ignore_case: Optional[bool] = None

    def __post_init__(self) -> None:
        _validate_tag(self.tag)
        _validate_value(self.value)
        if self.op not in _COMPARISON_OPERATORS:
            raise FilterError("unsupported filter operator: {!r}".format(self.op))
        if self.ignore_case is None:
            # contains/starts_with mirror search's case-insensitive matching;
            # equality mirrors find's case-sensitive matching.
            object.__setattr__(
                self, "ignore_case", self.op in ("contains", "starts_with")
            )

    def _matches(self, value: str) -> bool:
        if self.ignore_case:
            left, right = value.casefold(), self.value.casefold()
        else:
            left, right = value, self.value
        if self.op in ("==", "!="):
            return left == right
        if self.op == "contains":
            return right in left
        return left.startswith(right)

    def evaluate(self, song: Mapping[str, Any]) -> Optional[bool]:
        values = _song_values(song, self.tag)
        if values is None:
            return None
        matched = any(self._matches(value) for value in values)
        if self.op == "!=":
            return not matched
        return matched

    def to_mpd(self) -> str:
        return '({} {} "{}")'.format(self.tag, self.op, escape_value(self.value))


@dataclass(frozen=True)
class Not(Filter):
    child: Filter

    def evaluate(self, song: Mapping[str, Any]) -> Optional[bool]:
        result = self.child.evaluate(song)
        if result is None:
            return None
        return not result

    def to_mpd(self) -> str:
        return "(!{})".format(_parenthesized(self.child))


@dataclass(frozen=True)
class And(Filter):
    children: Tuple[Filter, ...]

    def __init__(self, *children: Filter) -> None:
        if not children:
            raise FilterError("And requires at least one condition")
        object.__setattr__(self, "children", tuple(children))

    def evaluate(self, song: Mapping[str, Any]) -> Optional[bool]:
        unknown = False
        for child in self.children:
            result = child.evaluate(song)
            if result is False:
                return False
            if result is None:
                unknown = True
        return None if unknown else True

    def to_mpd(self) -> str:
        return "({})".format(" AND ".join(_parenthesized(c) for c in self.children))


@dataclass(frozen=True)
class Or(Filter):
    children: Tuple[Filter, ...]

    def __init__(self, *children: Filter) -> None:
        if not children:
            raise FilterError("Or requires at least one condition")
        object.__setattr__(self, "children", tuple(children))

    def evaluate(self, song: Mapping[str, Any]) -> Optional[bool]:
        unknown = False
        for child in self.children:
            result = child.evaluate(song)
            if result is True:
                return True
            if result is None:
                unknown = True
        return None if unknown else False

    def to_mpd(self) -> str:
        return "({})".format(" OR ".join(_parenthesized(c) for c in self.children))


def _parenthesized(condition: Filter) -> str:
    text = condition.to_mpd()
    if text.startswith("(") and text.endswith(")"):
        return text
    return "({})".format(text)


def _song_values(song: Mapping[str, Any], tag: str) -> Optional[List[str]]:
    """Return all values for *tag*, or None when the field is unavailable."""

    if not isinstance(song, Mapping):
        return None
    key = tag.lower()
    if key == "any":
        values: List[str] = []
        for name, value in song.items():
            if str(name).lower() in ("file", "directory", "playlist"):
                continue
            values.extend(_as_values(value))
        return values if values else None
    if key in song:
        return _as_values(song[key])
    if tag in song:
        return _as_values(song[tag])
    return None


def _as_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str, ignore_case: bool) -> None:
        if not isinstance(text, str):
            raise TypeError("filter expression must be a string")
        if "\n" in text or "\r" in text:
            raise FilterError("filter expressions cannot contain newlines")
        self.text = text
        self.pos = 0
        self.ignore_case = ignore_case

    def parse(self) -> Filter:
        condition = self._parse_or()
        self._skip_space()
        if self.pos != len(self.text):
            raise FilterError(
                "unexpected trailing input at {}: {!r}".format(
                    self.pos, self.text[self.pos :]
                )
            )
        return condition

    def _skip_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _keyword(self, word: str) -> bool:
        self._skip_space()
        end = self.pos + len(word)
        if self.text[self.pos : end].upper() != word.upper():
            return False
        if end < len(self.text) and (
            self.text[end].isalnum() or self.text[end] == "_"
        ):
            return False
        self.pos = end
        return True

    def _parse_or(self) -> Filter:
        children = [self._parse_and()]
        while self._keyword("OR"):
            children.append(self._parse_and())
        return children[0] if len(children) == 1 else Or(*children)

    def _parse_and(self) -> Filter:
        children = [self._parse_unary()]
        while self._keyword("AND"):
            children.append(self._parse_unary())
        return children[0] if len(children) == 1 else And(*children)

    def _parse_unary(self) -> Filter:
        if self._keyword("NOT"):
            return Not(self._parse_unary())
        self._skip_space()
        if self.pos < len(self.text) and self.text[self.pos] == "!":
            self.pos += 1
            return Not(self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> Filter:
        self._skip_space()
        if self.pos < len(self.text) and self.text[self.pos] == "(":
            self.pos += 1
            condition = self._parse_or()
            self._skip_space()
            if self.pos >= len(self.text) or self.text[self.pos] != ")":
                raise FilterError("missing ')' at position {}".format(self.pos))
            self.pos += 1
            return condition
        tag = self._parse_tag()
        operator = self._parse_operator()
        value = self._parse_token("value")
        if operator in ("contains", "starts_with"):
            # The canonical MPD substring commands (search) are
            # case-insensitive.  Callers who need case-sensitive substring
            # matching construct the TagFilter directly.
            ignore_case = True
        else:
            ignore_case = self.ignore_case
        return TagFilter(tag, value, operator, ignore_case=ignore_case)

    def _parse_operator(self) -> str:
        self._skip_space()
        for operator in ("==", "!=", "="):
            if self.text.startswith(operator, self.pos):
                self.pos += len(operator)
                return "==" if operator == "=" else operator
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] == "_"
        ):
            self.pos += 1
        word = self.text[start : self.pos].lower()
        aliases = {
            "eq": "==",
            "ne": "!=",
            "contains": "contains",
            "starts_with": "starts_with",
            "startswith": "starts_with",
        }
        if word not in aliases:
            raise FilterError(
                "expected a comparison operator at position {}".format(start)
            )
        return aliases[word]

    def _parse_token(self, what: str) -> str:
        self._skip_space()
        if self.pos >= len(self.text):
            raise FilterError("expected {}".format(what))
        if self.text[self.pos] == '"':
            return self._parse_quoted()
        start = self.pos
        while self.pos < len(self.text) and not (
            self.text[self.pos].isspace() or self.text[self.pos] in "()"
        ):
            self.pos += 1
        token = self.text[start : self.pos]
        if not token:
            raise FilterError("expected {}".format(what))
        return token

    def _parse_tag(self) -> str:
        self._skip_space()
        if self.pos >= len(self.text):
            raise FilterError("expected tag")
        start = self.pos
        while self.pos < len(self.text) and not (
            self.text[self.pos].isspace()
            or self.text[self.pos] in "()=!"
        ):
            self.pos += 1
        token = self.text[start : self.pos]
        if not token:
            raise FilterError("expected tag")
        return token

    def _parse_quoted(self) -> str:
        self.pos += 1
        result: List[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == '"':
                return "".join(result)
            if char == "\\":
                if self.pos >= len(self.text):
                    raise FilterError("unterminated escape sequence")
                escaped = self.text[self.pos]
                self.pos += 1
                if escaped in ('"', "\\"):
                    result.append(escaped)
                else:
                    # Unknown escapes stay literal; in particular "\n" must
                    # not silently become a newline.
                    result.append("\\")
                    result.append(escaped)
            else:
                result.append(char)
        raise FilterError("unterminated quoted string")


def parse_filter(text: str, *, ignore_case: bool = False) -> Filter:
    """Parse an MPD-style filter expression into a :class:`Filter` tree.

    ``ignore_case`` mirrors the command that will evaluate the expression:
    ``find`` is case-sensitive, ``search`` is case-insensitive.  Only quotes
    and backslashes are escape sequences; an escaped quote or backslash stays
    part of the value and cannot introduce new operators.
    """

    return _Parser(text, ignore_case).parse()


def as_filter(
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]]
) -> Filter:
    """Coerce a convenient condition value into a :class:`Filter`."""

    if isinstance(condition, Filter):
        return condition
    if isinstance(condition, str):
        # A bare expression is interpreted with find's case-sensitive
        # semantics unless the caller wraps it in a Filter explicitly.
        return parse_filter(condition)
    if isinstance(condition, Mapping):
        return _filter_from_mapping(condition)
    if isinstance(condition, (list, tuple)):
        return _filter_from_sequence(condition)
    raise TypeError("cannot interpret {!r} as a filter".format(condition))


def _filter_from_mapping(condition: Mapping[str, Any]) -> Filter:
    if not condition:
        raise FilterError("empty condition")
    keys = {str(key).lower() for key in condition}
    if keys == {"and"}:
        return And(*(as_filter(part) for part in condition[_get_key(condition, "and")]))
    if keys == {"or"}:
        return Or(*(as_filter(part) for part in condition[_get_key(condition, "or")]))
    if keys == {"not"}:
        return Not(as_filter(condition[_get_key(condition, "not")]))
    if "tag" in keys and ("value" in keys or "what" in keys):
        tag = condition[_get_key(condition, "tag")]
        value = condition[_get_key(condition, "value", "what")]
        operator = condition.get(
            _get_key(condition, "op", "operator", default="op"), "=="
        )
        ignore_case = condition.get(
            _get_key(condition, "ignore_case", default="ignore_case"), None
        )
        if ignore_case is None:
            ignore_case = str(operator).lower() in ("contains", "starts_with")
        return TagFilter(
            str(tag),
            str(value),
            _normalize_operator(str(operator)),
            bool(ignore_case),
        )
    children: List[Filter] = []
    for tag, value in condition.items():
        if isinstance(value, (list, tuple, set, frozenset)):
            alternatives = [
                TagFilter(str(tag), str(item))
                for item in value
                if item is not None
            ]
            if not alternatives:
                raise FilterError("empty value list for tag {!r}".format(tag))
            children.append(
                alternatives[0] if len(alternatives) == 1 else Or(*alternatives)
            )
        elif value is None:
            children.append(Not(TagFilter(str(tag), "")))
        else:
            children.append(TagFilter(str(tag), str(value)))
    return children[0] if len(children) == 1 else And(*children)


def _get_key(
    mapping: Mapping[str, Any], *names: str, default: Optional[str] = None
) -> str:
    for name in names:
        for key in mapping:
            if str(key).lower() == name:
                return key
    if default is not None:
        return default
    raise FilterError("missing key {!r}".format(names[0]))


def _filter_from_sequence(condition: Sequence[Any]) -> Filter:
    if not condition:
        raise FilterError("empty condition")
    # A bare [tag, value] or [tag, operator, value] is one comparison.
    if len(condition) in (2, 3) and all(
        isinstance(part, str) for part in condition
    ):
        if len(condition) == 2:
            return TagFilter(condition[0], condition[1])
        return TagFilter(condition[0], condition[2], _normalize_operator(condition[1]))
    # Otherwise a sequence of conditions combined with AND.
    return And(*(as_filter(part) for part in condition))


def normalize_filter(condition: Filter) -> Filter:
    """Normalize ``!=`` into ``Not(==)`` so NOT scope is explicit.

    For a multi-valued tag this keeps the original meaning: ``tag != value`` is
    true when no value of the tag equals *value*.
    """

    if isinstance(condition, TagFilter):
        if condition.op == "!=":
            positive = TagFilter(
                condition.tag,
                condition.value,
                "==",
                ignore_case=condition.ignore_case,
            )
            return Not(positive)
        return condition
    if isinstance(condition, Not):
        child = normalize_filter(condition.child)
        if isinstance(child, Not):
            return child.child
        return Not(child)
    if isinstance(condition, And):
        return And(*(normalize_filter(child) for child in condition.children))
    if isinstance(condition, Or):
        return Or(*(normalize_filter(child) for child in condition.children))
    raise TypeError("not a filter: {!r}".format(condition))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerQuery:
    """One command sent to MPD while collecting candidate records."""

    command: str
    args: Tuple[Any, ...] = ()
    reason: str = ""

    def as_command(self) -> str:
        return " ".join((self.command,) + tuple(str(arg) for arg in self.args))

    def __post_init__(self) -> None:
        for arg in self.args:
            if "\n" in str(arg) or "\r" in str(arg):
                raise FilterError("server arguments cannot contain newlines")


@dataclass
class FilterPlan:
    """A version-specific plan for one condition.

    ``queries`` collect a superset of every possible match.  When
    ``residual`` is not None, the original condition must be evaluated locally
    against each returned record.  ``unsupported`` explains guarantees that
    the server cannot provide.
    """

    condition: Filter
    version: Optional[str]
    server_supported: bool
    queries: Tuple[ServerQuery, ...]
    residual: Optional[Filter]
    unsupported: Tuple[str, ...] = ()
    candidate_superset: bool = True
    #: The condition exactly as supplied by the caller.  This may be a string
    #: or mapping and is not normalized; use ``condition`` for the AST.
    original: Optional[Union[Filter, str, Mapping[str, Any], Sequence[Any]]] = None
    #: Recheck every returned record locally.  Keeping this on by default
    #: makes the confirmed set independent of the server version, including
    #: for negations and multi-valued tags.
    verify_locally: bool = True

    @property
    def requires_local_verification(self) -> bool:
        """Whether the server query alone cannot produce the final set."""

        return self.residual is not None

    @property
    def rechecks_locally(self) -> bool:
        """Whether every returned record is rechecked before classification."""

        return self.verify_locally or self.residual is not None

    @property
    def mode(self) -> str:
        return "server" if self.server_supported else "candidate"

    @property
    def supported(self) -> bool:
        return self.server_supported

    @property
    def guarantee(self) -> str:
        """Describe the strength of the server query's result."""

        if self.server_supported:
            return "exact"
        return "candidate superset with local verification"

    @property
    def exhaustive(self) -> bool:
        """Whether the candidate query returned every record the server has."""

        return any(query.command == "listallinfo" for query in self.queries)

    @property
    def query_complete(self) -> bool:
        """Whether absence from the server result proves non-membership.

        A native filter expression or an exact legacy find/search query has
        the same matching semantics as the condition, so the server's
        omission is authoritative.  A narrowed candidate from a mixed
        condition is not.  When the condition has a local residual, every
        candidate is inspected, so a full scan is also authoritative.
        """

        if self.server_supported or not self.candidate_superset:
            return True
        return self.residual is not None and self.exhaustive

    def as_commands(self) -> List[str]:
        return [query.as_command() for query in self.queries]


def parse_version(version: Optional[str]) -> Optional[Tuple[int, ...]]:
    if not version:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", str(version))
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def supports_filter_expressions(version: Optional[str]) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= FILTER_EXPRESSION_VERSION


def compile_filter(
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    version: Optional[str] = None,
    *,
    require_server_only: bool = False,
) -> FilterPlan:
    """Return the server query plan for *condition* on MPD *version*.

    With ``require_server_only`` the function raises
    :class:`FilterNotSupported` instead of returning a plan whose result would
    still need local verification; the exception carries the fallback plan.
    """

    original = condition
    parsed = normalize_filter(as_filter(condition))
    if supports_filter_expressions(version):
        if _modern_expressible(parsed, version):
            plan = FilterPlan(
                condition=parsed,
                version=version,
                server_supported=True,
                queries=(_modern_query(parsed, version),),
                residual=None,
            )
        else:
            plan = _candidate_plan(
                parsed,
                version,
                modern=True,
                reason=_modern_fallback_reason(parsed, version),
            )
    else:
        known_version = parse_version(version) is not None
        if known_version and _legacy_expressible(parsed):
            plan = FilterPlan(
                condition=parsed,
                version=version,
                server_supported=True,
                queries=tuple(
                    _candidate_queries(parsed, version, modern=False)
                ),
                residual=None,
                verify_locally=True,
            )
        else:
            plan = _candidate_plan(
                parsed,
                version,
                modern=False,
                reason=(
                    "MPD {} has no filter expressions; using legacy find/search "
                    "candidates and local verification".format(
                        version or "(unknown version)"
                    )
                ),
                unknown_version=not known_version,
            )
    plan.original = original
    if require_server_only and not plan.server_supported:
        raise FilterNotSupported(
            "condition cannot be executed completely server-side: "
            + "; ".join(plan.unsupported),
            plan,
        )
    return plan


def _modern_fallback_reason(condition: Filter, version: Optional[str]) -> str:
    if _uses_operator(condition, "starts_with") and not supports_operator(
        "starts_with", version
    ):
        return (
            "MPD {} does not support the starts_with filter operator; using "
            "contains as a candidate superset and local verification".format(
                version
            )
        )
    return (
        "the condition mixes case-sensitive and case-insensitive comparisons, "
        "which one MPD command cannot express; using a candidate superset and "
        "local verification"
    )


def _leaf_mode(leaf: TagFilter) -> Optional[str]:
    """Return the MPD command whose semantics equal this leaf, if any."""

    if leaf.op not in ("==", "!=", "contains", "starts_with"):
        return None
    return "search" if leaf.ignore_case else "find"


def _modern_modes(condition: Filter) -> Optional[Set[str]]:
    if isinstance(condition, TagFilter):
        mode = _leaf_mode(condition)
        return None if mode is None else {mode}
    if isinstance(condition, Not):
        return _modern_modes(condition.child)
    if isinstance(condition, (And, Or)):
        modes: Set[str] = set()
        for child in condition.children:
            child_modes = _modern_modes(child)
            if child_modes is None:
                return None
            modes |= child_modes
        return modes
    return None


def _modern_expressible(condition: Filter, version: Optional[str]) -> bool:
    """Whether one native filter expression implements the condition."""

    modes = _modern_modes(condition)
    # Mixing find and search semantics would change one side of the
    # condition, so such a condition needs a local residual.
    if modes is None or len(modes) != 1:
        return False
    if _uses_operator(condition, "starts_with") and not supports_operator(
        "starts_with", version
    ):
        return False
    return True


def supports_operator(operator: str, version: Optional[str]) -> bool:
    """Whether *version* supports *operator* inside a filter expression."""

    if operator == "starts_with":
        parsed = parse_version(version)
        return parsed is not None and parsed >= STARTS_WITH_VERSION
    return True


def _uses_operator(condition: Filter, operator: str) -> bool:
    if isinstance(condition, TagFilter):
        return condition.op == operator
    if isinstance(condition, Not):
        return _uses_operator(condition.child, operator)
    if isinstance(condition, (And, Or)):
        return any(_uses_operator(child, operator) for child in condition.children)
    return False


def _modern_query(condition: Filter, version: Optional[str]) -> ServerQuery:
    modes = _modern_modes(condition)
    command = "search" if modes == {"search"} else "find"
    return ServerQuery(command, (condition.to_mpd(),), "native filter expression")


def _legacy_family(leaf: TagFilter) -> Optional[str]:
    """Return the legacy command that implements this leaf exactly."""

    if leaf.op == "==" and not leaf.ignore_case:
        return "find"
    if leaf.op == "contains" and leaf.ignore_case:
        return "search"
    return None


def _legacy_expressible(condition: Filter) -> bool:
    """Whether one legacy command implements the exact condition alone.

    Legacy ``find`` and ``search`` accept multiple tag/value pairs combined
    with AND.  A conjunction is exact when every leaf belongs to one family
    (exact find or substring search) and no tag is repeated, so each pair has
    one unambiguous comparison.
    """

    if isinstance(condition, TagFilter):
        return _legacy_family(condition) is not None
    if not isinstance(condition, And):
        return False
    families: Set[str] = set()
    tags: Set[str] = set()
    for child in condition.children:
        if not isinstance(child, TagFilter):
            return False
        family = _legacy_family(child)
        if family is None:
            return False
        families.add(family)
        tag = child.tag.lower()
        if tag in tags:
            return False
        tags.add(tag)
    return len(families) == 1


def _candidate_plan(
    condition: Filter,
    version: Optional[str],
    *,
    modern: bool,
    reason: str,
    unknown_version: bool = False,
) -> FilterPlan:
    """Build a superset query plan with *condition* as local residual."""
    queries = _candidate_queries(condition, version, modern=modern)
    unsupported: List[str] = []
    if unknown_version:
        unsupported.append(
            "MPD version is unknown; server version support was not assumed"
        )
    unsupported.append(reason)
    if not queries:
        unsupported.append("the condition is unsatisfiable and needs no query")
    return FilterPlan(
        condition=condition,
        version=version,
        server_supported=False,
        queries=tuple(queries),
        residual=condition,
        unsupported=tuple(unsupported),
        candidate_superset=True,
    )


def _candidate_queries(
    condition: Filter, version: Optional[str], *, modern: bool
) -> List[ServerQuery]:
    """Build queries whose union covers every match of *condition*."""

    terms = _candidate_terms(condition)
    queries: List[ServerQuery] = []
    seen: Set[Tuple[Any, ...]] = set()
    for term in terms:
        query = _query_for_term(term, modern=modern, version=version)
        key = (query.command,) + query.args
        if key not in seen:
            seen.add(key)
            queries.append(query)
    return queries


def _candidate_terms(condition: Filter) -> List[Tuple[TagFilter, ...]]:
    """Return positive conjunctive terms whose union covers all matches.

    A negated or otherwise unrepresentable subexpression contributes an empty
    term, meaning "no narrowing constraint".  An empty term ultimately makes
    the candidate a full scan, which is always a safe superset.
    """

    if isinstance(condition, TagFilter):
        if condition.op == "!=":
            return [()]
        return [(condition,)]
    if isinstance(condition, Not):
        return [()]
    if isinstance(condition, And):
        terms: List[Tuple[TagFilter, ...]] = [()]
        for child in condition.children:
            child_terms = _candidate_terms(child)
            next_terms: List[Tuple[TagFilter, ...]] = []
            for left in terms:
                for right in child_terms:
                    next_terms.append(left + right)
                    if len(next_terms) > MAX_CANDIDATE_TERMS:
                        return [()]
            terms = next_terms
        return terms
    if isinstance(condition, Or):
        terms = []
        for child in condition.children:
            terms.extend(_candidate_terms(child))
            if len(terms) > MAX_CANDIDATE_TERMS or any(not term for term in terms):
                return [()]
        return terms
    return [()]


def _unique_by_tag(leaves: Iterable[TagFilter]) -> List[TagFilter]:
    """Keep at most one predicate per tag.

    A record satisfying every predicate also satisfies the first one, so
    dropping duplicates only broadens the candidate set.
    """

    result: List[TagFilter] = []
    seen: Set[str] = set()
    for leaf in leaves:
        key = leaf.tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(leaf)
    return result


def _query_for_term(
    term: Tuple[TagFilter, ...], *, modern: bool, version: Optional[str]
) -> ServerQuery:
    if not term:
        return ServerQuery(
            "listallinfo", (), "full scan for negated/compound condition"
        )

    exact = [leaf for leaf in term if leaf.op == "==" and not leaf.ignore_case]
    searchable = [
        leaf
        for leaf in term
        if leaf.op in ("contains", "starts_with") or leaf.ignore_case
    ]

    if modern:
        term = tuple(
            TagFilter(leaf.tag, leaf.value, "contains", leaf.ignore_case)
            if leaf.op == "starts_with" and not supports_operator(
                "starts_with", version
            )
            else leaf
            for leaf in term
        )
        # Each command must keep its own case semantics.  find carries only
        # case-sensitive leaves and search only case-insensitive ones; the
        # strongest side becomes the candidate and the other side is verified
        # locally.
        find_leaves = _unique_by_tag(
            leaf for leaf in term if _leaf_mode(leaf) == "find"
        )
        search_leaves = _unique_by_tag(
            leaf for leaf in term if _leaf_mode(leaf) == "search"
        )
        if find_leaves:
            return ServerQuery(
                "find",
                (_and_expression(find_leaves),),
                "case-sensitive candidate filter expression",
            )
        if search_leaves:
            return ServerQuery(
                "search",
                (_and_expression(search_leaves),),
                "case-insensitive candidate filter expression",
            )
        return ServerQuery("listallinfo", (), "no expressible positive comparison")

    if exact:
        # Legacy find accepts repeated tag/value pairs, ANDed together.
        return ServerQuery(
            "find",
            _legacy_args(_unique_by_tag(exact)),
            "exact legacy find candidate",
        )
    if searchable:
        # Legacy search is case-insensitive substring matching.  It is a
        # superset for starts_with and case-sensitive contains, never a
        # substitute for exact find.
        return ServerQuery(
            "search",
            _legacy_args(_unique_by_tag(searchable)),
            "loose legacy search candidate",
        )
    return ServerQuery("listallinfo", (), "no expressible positive comparison")


def _and_expression(leaves: List[TagFilter]) -> str:
    if len(leaves) == 1:
        return leaves[0].to_mpd()
    return "({})".format(" AND ".join(leaf.to_mpd() for leaf in leaves))


def _legacy_args(leaves: List[TagFilter]) -> Tuple[Any, ...]:
    args: List[Any] = []
    for leaf in leaves:
        args.extend((leaf.tag, leaf.value))
    return tuple(args)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    """Outcome of evaluating one condition against one MPD server.

    ``confirmed`` records satisfy the original condition.  ``undetermined``
    records could not be decided because a required field was unavailable;
    they are neither confirmed nor excluded.  ``excluded`` records are known
    not to satisfy it.
    """

    condition: Filter
    plan: FilterPlan
    confirmed: List[Mapping[str, Any]]
    undetermined: List[Mapping[str, Any]]
    excluded: List[Mapping[str, Any]]
    executed: Tuple[ServerQuery, ...]

    def __iter__(self) -> Any:
        """Iterate over confirmed records, like a filtered find result."""

        return iter(self.confirmed)

    def __len__(self) -> int:
        return len(self.confirmed)

    @property
    def original_condition(self) -> Any:
        """Return the condition value exactly as supplied by the caller."""

        return self.plan.original

    @property
    def records(self) -> List[Mapping[str, Any]]:
        return self.confirmed

    @property
    def complete(self) -> bool:
        """Whether no returned record remains undecided."""

        return not self.undetermined

    @property
    def authoritative(self) -> bool:
        """Whether absence from the result proves exclusion.

        A narrowed candidate query can hide records it never returned, so a
        complete classification is authoritative only when the query itself
        was exhaustive or exactly matched the condition.
        """

        return self.complete and self.plan.query_complete

    @property
    def unsupported(self) -> Tuple[str, ...]:
        return self.plan.unsupported

    @property
    def supported(self) -> bool:
        return self.plan.server_supported

    @property
    def confirmed_files(self) -> List[str]:
        return [str(song.get("file", "")) for song in self.confirmed]

    @property
    def undetermined_files(self) -> List[str]:
        return [str(song.get("file", "")) for song in self.undetermined]

    @property
    def excluded_files(self) -> List[str]:
        return [str(song.get("file", "")) for song in self.excluded]

    @property
    def guarantee(self) -> str:
        return self.plan.guarantee

    def as_dict(self) -> Dict[str, Any]:
        """Return a transport-friendly summary of the outcome."""

        return {
            "confirmed": self.confirmed_files,
            "undetermined": self.undetermined_files,
            "excluded": self.excluded_files,
            "supported": self.plan.server_supported,
            "guarantee": self.plan.guarantee,
            "authoritative": self.authoritative,
            "unsupported": list(self.plan.unsupported),
            "commands": [query.as_command() for query in self.executed],
        }


def select_songs(
    client: Any,
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    *,
    require_server_only: bool = False,
) -> FilterResult:
    """Execute *condition* through a synchronous MPD client."""

    plan = compile_filter(
        condition,
        getattr(client, "mpd_version", None),
        require_server_only=require_server_only,
    )
    return _classify(plan.condition, plan, _run_queries(client, plan))


async def select_songs_async(
    client: Any,
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    *,
    require_server_only: bool = False,
) -> FilterResult:
    """Execute *condition* through an asyncio MPD client."""

    plan = compile_filter(
        condition,
        getattr(client, "mpd_version", None),
        require_server_only=require_server_only,
    )
    parsed = plan.condition
    candidates: List[Mapping[str, Any]] = []
    seen: Set[str] = set()
    for query in plan.queries:
        method = getattr(client, query.command, None)
        if method is None:
            raise FilterNotSupported(
                "client does not implement {!r}".format(query.command), plan
            )
        result = method(*query.args)
        if hasattr(result, "__await__"):
            result = await result
        _collect(result, candidates, seen)
    return _classify(parsed, plan, candidates)


def _run_queries(client: Any, plan: FilterPlan) -> List[Mapping[str, Any]]:
    candidates: List[Mapping[str, Any]] = []
    seen: Set[str] = set()
    for query in plan.queries:
        method = getattr(client, query.command, None)
        if method is None:
            raise FilterNotSupported(
                "client does not implement {!r}".format(query.command), plan
            )
        _collect(method(*query.args), candidates, seen)
    return candidates


def collect_candidates(
    results: Iterable[Iterable[Mapping[str, Any]]],
) -> List[Mapping[str, Any]]:
    """Merge query results into one de-duplicated candidate list."""

    candidates: List[Mapping[str, Any]] = []
    seen: Set[str] = set()
    for result in results:
        _collect(result, candidates, seen)
    return candidates


def classify_candidates(
    plan: FilterPlan,
    candidates: Iterable[Mapping[str, Any]],
) -> FilterResult:
    """Apply *plan* to candidate records and return the final result."""

    return _classify(plan.condition, plan, list(candidates))


def _collect(
    result: Iterable[Mapping[str, Any]],
    candidates: List[Mapping[str, Any]],
    seen: Set[str],
) -> None:
    if result is None:
        return
    for song in result:
        if not isinstance(song, Mapping) or "file" not in song:
            # listallinfo also reports directories and playlists.
            continue
        key = str(song["file"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(song)


def _classify(
    condition: Filter,
    plan: FilterPlan,
    candidates: List[Mapping[str, Any]],
) -> FilterResult:
    confirmed: List[Mapping[str, Any]] = []
    undetermined: List[Mapping[str, Any]] = []
    excluded: List[Mapping[str, Any]] = []
    if plan.verify_locally or plan.residual is not None:
        residual = plan.residual if plan.residual is not None else condition
        for song in candidates:
            result = residual.evaluate(song)
            if result is True:
                confirmed.append(song)
            elif result is False:
                excluded.append(song)
            else:
                undetermined.append(song)
    else:
        confirmed.extend(candidates)
    return FilterResult(
        condition=condition,
        plan=plan,
        confirmed=confirmed,
        undetermined=undetermined,
        excluded=excluded,
        executed=plan.queries,
    )


def evaluate_filter(
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    song: Mapping[str, Any],
) -> Optional[bool]:
    """Evaluate *condition* against one song without contacting a server.

    Returns None when a field required by the condition is unavailable.
    """

    return normalize_filter(as_filter(condition)).evaluate(song)


def evaluate_records(
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    records: Iterable[Mapping[str, Any]],
) -> FilterResult:
    """Classify records using only local evaluation (no server query)."""

    original = condition
    parsed = normalize_filter(as_filter(condition))
    confirmed: List[Mapping[str, Any]] = []
    undetermined: List[Mapping[str, Any]] = []
    excluded: List[Mapping[str, Any]] = []
    for song in records:
        result = parsed.evaluate(song)
        if result is True:
            confirmed.append(song)
        elif result is False:
            excluded.append(song)
        else:
            undetermined.append(song)
    plan = FilterPlan(
        condition=parsed,
        version=None,
        server_supported=True,
        queries=(),
        residual=None,
        unsupported=(),
        original=original,
        verify_locally=True,
        candidate_superset=False,
    )
    return FilterResult(
        condition=parsed,
        plan=plan,
        confirmed=confirmed,
        undetermined=undetermined,
        excluded=excluded,
        executed=(),
    )


# Convenience aliases for callers that describe the operation rather than the
# syntax.  All of them share the semantics documented above.
plan_filter = compile_filter
build_plan = compile_filter
filter_songs = select_songs
find_filter = select_songs
search_filter = select_songs
apply_filter = evaluate_records
classify_records = evaluate_records
