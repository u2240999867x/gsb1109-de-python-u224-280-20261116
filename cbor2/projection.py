"""Row-oriented projections over decoded CBOR values.

A *projection* turns a decoded CBOR value into a table.  Each output column is
described by a path into the value; arrays at expansion points become rows.
Two combination modes are supported:

``paired``
    Divergent branches at the same point are aligned by occurrence index, like
    :func:`zip`.  When their occurrence counts differ, the missing policy
    decides whether the shorter branch is padded, the excess is skipped, or
    the plan is rejected.

``cross``
    Divergent branches at the same point form a Cartesian product, ordered
    with the rightmost branch varying fastest.

Paths share a common prefix structurally.  ``orders[].id`` and
``orders[].items[].sku`` therefore expand each order's items *within that
order*; orders are not crossed with each other.

The API is plan-first.  :meth:`Projection.plan` computes the exact row count
and the provenance of every column without returning records.  Records are
returned only from :meth:`Projection.project`, and only after the full row set
has been built, so an exceeded budget or a generation error can never leak a
partially expanded result.

Shared values (CBOR tags 28 and 29) are never deduplicated.  Each place where a
path reaches an object is a separate occurrence; if the same object is
referenced twice, both references contribute rows.  Only a recursive expansion
that actually follows a cycle has no finite endpoint.  That case raises
:class:`ProjectionCycleError` with the concrete path where the cycle was
followed and the path where the same object was entered; it is never expanded
forever and never reported as zero rows.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from ._types import CBORError

if TYPE_CHECKING:
    from ._types import CBORTag


_TAG_TYPES: tuple[type, ...] | None = None


def _tag_type() -> tuple[type, ...]:
    """Return the known CBORTag classes (pure Python and C extension)."""
    global _TAG_TYPES
    if _TAG_TYPES is None:
        from . import CBORTag
        from ._types import CBORTag as PythonCBORTag

        _TAG_TYPES = (CBORTag,) if CBORTag is PythonCBORTag else (CBORTag, PythonCBORTag)
    return _TAG_TYPES


__all__ = [
    "Column",
    "ColumnSource",
    "EmptyPolicy",
    "ExpansionMode",
    "MissingPolicy",
    "Path",
    "PathStep",
    "Projection",
    "ProjectionBudgetError",
    "ProjectionCardinalityError",
    "ProjectionCycleError",
    "ProjectionError",
    "ProjectionPathError",
    "ProjectionPlan",
    "ProjectionPolicy",
    "ProjectionResult",
    "ProjectionTagError",
    "TagPolicy",
    "parse_path",
    "plan_projection",
    "project",
    "project_rows",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNSET = object()


class ExpansionMode(str, Enum):
    """How divergent branches at the same point are combined into rows."""

    PAIRED = "paired"
    CROSS = "cross"


class MissingPolicy(str, Enum):
    """What to do when a path step does not exist.

    ``NULL`` contributes one occurrence filled with the configured missing
    value, ``SKIP`` contributes no occurrence, and ``ERROR`` aborts the plan.
    """

    NULL = "null"
    SKIP = "skip"
    ERROR = "error"


class EmptyPolicy(str, Enum):
    """What to do when an array at an expansion point is empty.

    ``ZERO`` contributes no occurrences, ``NULL`` contributes one missing
    occurrence, and ``ERROR`` aborts the plan.
    """

    ZERO = "zero"
    NULL = "null"
    ERROR = "error"


class TagPolicy(str, Enum):
    """What to do with a tag encountered before the end of a path.

    ``OPAQUE`` keeps the tag as a single occurrence, ``UNWRAP`` continues into
    the tag's value, ``ZERO`` drops the occurrence, and ``ERROR`` aborts the
    plan.
    """

    OPAQUE = "opaque"
    UNWRAP = "unwrap"
    ZERO = "zero"
    ERROR = "error"


class ProjectionError(CBORError):
    """Base class for projection errors."""


class ProjectionPathError(ProjectionError, ValueError):
    """Raised for an invalid projection path."""


class ProjectionCycleError(ProjectionError, ValueError):
    """Raised when a recursive projection follows a cycle forever.

    :ivar path: the path at which the cycle was followed again
    :ivar cycle_path: the earlier path where the same object was entered
    :ivar value: the repeated object
    """

    def __init__(self, path: str, cycle_path: str, value: Any) -> None:
        self.path = path
        self.cycle_path = cycle_path
        self.value = value
        super().__init__(
            f"projection path {path!r} recurses back to {cycle_path!r} and has no finite endpoint"
        )


class ProjectionBudgetError(ProjectionError):
    """Raised when the exact row count exceeds the caller's budget.

    :ivar row_count: number of rows the projection would produce
    :ivar max_rows: the caller's budget
    :ivar plan: the plan computed before the budget check
    """

    def __init__(self, row_count: int, max_rows: int, plan: ProjectionPlan) -> None:
        self.row_count = row_count
        self.max_rows = max_rows
        self.plan = plan
        super().__init__(
            f"projection would produce {row_count} rows, which exceeds the "
            f"budget of {max_rows} rows; no records were emitted"
        )


class ProjectionCardinalityError(ProjectionError, ValueError):
    """Raised when a cardinality policy rejects a value."""


class ProjectionTagError(ProjectionError, ValueError):
    """Raised when an unresolvable tag is rejected by the tag policy."""


@dataclass(frozen=True)
class PathStep:
    """One step of a projection path.

    ``kind`` is ``"key"`` (mapping key), ``"index"`` (array index),
    ``"expand"`` (expand one array level) or ``"recursive"`` (recursively
    expand nested arrays until a non-array value is reached).
    """

    kind: str
    value: Any = None

    def __post_init__(self) -> None:
        normalized = self.kind.lower()
        if normalized not in ("key", "index", "expand", "recursive"):
            raise ProjectionPathError(f"unknown path step kind {self.kind!r}")

        if normalized == "index":
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                raise ProjectionPathError("index path steps require an integer value")
            if self.value < 0:
                raise ProjectionPathError("index path steps cannot be negative")
        elif normalized in ("expand", "recursive"):
            if self.value is not None:
                raise ProjectionPathError(f"{normalized} path steps take no value")
        elif self.value is None:
            raise ProjectionPathError("key path steps require a value")

        object.__setattr__(self, "kind", normalized)


@dataclass(frozen=True)
class Path:
    """A parsed projection path."""

    steps: tuple[PathStep, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def expands(self) -> bool:
        """Whether this path contains any expansion step."""
        return any(step.kind in ("expand", "recursive") for step in self.steps)

    @property
    def recursive(self) -> bool:
        """Whether this path contains a recursive expansion step."""
        return any(step.kind == "recursive" for step in self.steps)

    def __str__(self) -> str:
        if not self.steps:
            return "$"

        parts: list[str] = []
        for step in self.steps:
            if step.kind == "expand":
                parts.append("[]")
            elif step.kind == "recursive":
                parts.append("[**]")
            elif step.kind == "index":
                parts.append(f"[{step.value}]")
            elif isinstance(step.value, str) and _IDENTIFIER_RE.match(step.value):
                parts.append(f".{step.value}" if parts else step.value)
            else:
                parts.append(f"[{step.value!r}]")

        return "".join(parts)

    def __repr__(self) -> str:
        return f"Path({str(self)!r})"


def _parse_string_path(path: str) -> Path:
    text = path.strip()
    if text.startswith("$"):
        text = text[1:]
        if text.startswith(".") or text.startswith("/"):
            text = text[1:]

    if not text:
        return Path()

    steps: list[PathStep] = []
    key_buffer: list[str] = []
    index = 0
    length = len(text)

    def flush_key() -> None:
        if key_buffer:
            steps.append(PathStep("key", "".join(key_buffer)))
            key_buffer.clear()

    while index < length:
        char = text[index]
        if char in "./":
            flush_key()
            index += 1
        elif char == "\\":
            if index + 1 >= length:
                raise ProjectionPathError(f"dangling escape in path {path!r}")
            key_buffer.append(text[index + 1])
            index += 2
        elif char == "[":
            flush_key()
            end = text.find("]", index + 1)
            if end < 0:
                raise ProjectionPathError(f"unclosed '[' in path {path!r}")

            inside = text[index + 1 : end].strip()
            if inside in ("", "*"):
                steps.append(PathStep("expand"))
            elif inside == "**":
                steps.append(PathStep("recursive"))
            elif inside.isdigit():
                steps.append(PathStep("index", int(inside)))
            elif len(inside) >= 2 and inside[0] == inside[-1] and inside[0] in "'\"":
                steps.append(PathStep("key", inside[1:-1]))
            else:
                raise ProjectionPathError(
                    f"invalid bracket expression [{inside}] in path {path!r}"
                )
            index = end + 1
        else:
            key_buffer.append(char)
            index += 1

    flush_key()
    return Path(tuple(steps))


def parse_path(path: str | Path | Sequence[Any]) -> Path:
    """Parse a path specification.

    String paths use dots (or slashes) between mapping keys, ``[N]`` for an
    array index, ``[]`` or ``[*]`` to expand one array level, and ``[**]`` to
    expand nested arrays recursively.  A literal dot or slash in a key can be
    escaped with a backslash, or the key can be written as
    ``["key.with.dots"]``.  A leading ``$`` is optional.

    A sequence is interpreted literally: strings are mapping keys, integers are
    array indices, ``None``, ``"*"`` or ``...`` expand one array level, and
    ``"**"`` expands recursively.  This form supports non-string keys such as
    :class:`bytes`.
    """
    if isinstance(path, Path):
        return path
    if isinstance(path, (bytes, bytearray, memoryview)):
        raise ProjectionPathError("a path cannot be a bytes object")
    if isinstance(path, str):
        return _parse_string_path(path)

    try:
        items = tuple(path)
    except TypeError:
        raise ProjectionPathError(f"invalid path {path!r}") from None

    steps: list[PathStep] = []
    for item in items:
        if isinstance(item, PathStep):
            steps.append(item)
        elif item is None or item is Ellipsis:
            steps.append(PathStep("expand"))
        elif isinstance(item, str) and item == "*":
            steps.append(PathStep("expand"))
        elif isinstance(item, str) and item == "**":
            steps.append(PathStep("recursive"))
        elif isinstance(item, int) and not isinstance(item, bool):
            steps.append(PathStep("index", item))
        else:
            steps.append(PathStep("key", item))

    return Path(tuple(steps))


def _coerce_enum(enum_type: type[Enum], value: Any, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        try:
            return enum_type(str(value).lower())
        except (TypeError, ValueError):
            choices = ", ".join(repr(member.value) for member in enum_type)
            raise ValueError(
                f"invalid {field_name} policy {value!r} (choose from {choices})"
            ) from None


@dataclass(frozen=True)
class ProjectionPolicy:
    """Policies that give a projection a finite, well-defined row set.

    :param missing: treatment of a path step that does not exist
    :param empty: treatment of an empty array at an expansion point
    :param tag: treatment of a tag encountered before the end of a path
    :param missing_value: value used for ``MissingPolicy.NULL`` and
        ``EmptyPolicy.NULL`` occurrences
    :param tag_resolver: optional callable that turns a :class:`~cbor2.CBORTag`
        into a traversable value.  Returning the tag itself, or raising an
        exception, means the tag is unresolvable and the ``tag`` policy
        applies.  The callable must be deterministic because it is also used
        while planning.
    :param auto_expand: when true, looking up a mapping key on an array applies
        the lookup to every element; an explicit ``[]`` step is not required
        for this common shape.
    """

    missing: MissingPolicy | str = MissingPolicy.NULL
    empty: EmptyPolicy | str = EmptyPolicy.ZERO
    tag: TagPolicy | str = TagPolicy.OPAQUE
    missing_value: Any = None
    tag_resolver: Callable[[CBORTag], Any] | None = None
    auto_expand: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", _coerce_enum(MissingPolicy, self.missing, "missing"))
        object.__setattr__(self, "empty", _coerce_enum(EmptyPolicy, self.empty, "empty"))
        object.__setattr__(self, "tag", _coerce_enum(TagPolicy, self.tag, "tag"))
        if self.tag_resolver is not None and not callable(self.tag_resolver):
            raise TypeError("tag_resolver must be callable or None")
        if not isinstance(self.auto_expand, bool):
            raise TypeError("auto_expand must be a boolean")


@dataclass(frozen=True)
class Column:
    """One output column of a projection.

    :param name: output column name
    :param path: source path (see :func:`parse_path`)
    :param missing: optional per-column override of the missing policy
    :param empty: optional per-column override of the empty-array policy
    :param tag: optional per-column override of the tag policy
    """

    name: str
    path: str | Path | Sequence[Any]
    missing: MissingPolicy | str | None = None
    empty: EmptyPolicy | str | None = None
    tag: TagPolicy | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("column names must be non-empty strings")

        object.__setattr__(self, "path", parse_path(self.path))
        if self.missing is not None:
            object.__setattr__(
                self, "missing", _coerce_enum(MissingPolicy, self.missing, "missing")
            )
        if self.empty is not None:
            object.__setattr__(self, "empty", _coerce_enum(EmptyPolicy, self.empty, "empty"))
        if self.tag is not None:
            object.__setattr__(self, "tag", _coerce_enum(TagPolicy, self.tag, "tag"))


@dataclass(frozen=True)
class ColumnSource:
    """Provenance and observed cardinality of one output column.

    :ivar name: output column name
    :ivar path: canonical source path
    :ivar segments: parsed path steps
    :ivar expands: whether the path expands at least one array
    :ivar occurrences: number of values evaluated for this column
    :ivar missing: number of missing occurrences resolved by the missing policy
    :ivar empty: number of empty arrays resolved by the empty policy
    :ivar tags: number of tags resolved by the tag policy
    """

    name: str
    path: str
    segments: tuple[PathStep, ...]
    expands: bool
    occurrences: int
    missing: int = 0
    empty: int = 0
    tags: int = 0

    @property
    def source(self) -> str:
        """Alias for :attr:`path`."""
        return self.path

    @property
    def cardinality(self) -> int:
        """Alias for :attr:`occurrences`."""
        return self.occurrences

    def as_dict(self) -> dict[str, Any]:
        """Return this provenance record as a plain dictionary."""
        return {
            "name": self.name,
            "path": self.path,
            "segments": self.segments,
            "expands": self.expands,
            "occurrences": self.occurrences,
            "missing": self.missing,
            "empty": self.empty,
            "tags": self.tags,
        }

    def __getitem__(self, key: str) -> Any:
        try:
            return self.as_dict()[key]
        except KeyError:
            raise KeyError(key) from None

    def __repr__(self) -> str:
        return (
            f"ColumnSource(name={self.name!r}, path={self.path!r}, occurrences={self.occurrences})"
        )


@dataclass(frozen=True)
class ProjectionPlan:
    """An exact description of a projection's shape.

    :ivar columns: source information for every output column, in output order
    :ivar row_count: exact number of records that will be produced
    :ivar mode: expansion mode used to combine divergent branches
    :ivar policy: cardinality policy used by the projection
    :ivar finite: always ``True`` for a successfully built plan
    """

    columns: tuple[ColumnSource, ...]
    row_count: int
    mode: ExpansionMode
    policy: ProjectionPolicy
    finite: bool = True

    @property
    def column_names(self) -> tuple[str, ...]:
        """Output column names."""
        return tuple(column.name for column in self.columns)

    @property
    def sources(self) -> dict[str, str]:
        """Mapping of output column names to their canonical source paths."""
        return {column.name: column.path for column in self.columns}

    @property
    def rows(self) -> int:
        """Alias for :attr:`row_count`."""
        return self.row_count

    @property
    def column_sources(self) -> tuple[ColumnSource, ...]:
        """Alias for :attr:`columns`."""
        return self.columns

    def __getitem__(self, name: str) -> ColumnSource:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    def ensure_within(self, max_rows: int) -> ProjectionPlan:
        """Return this plan if it fits the budget, otherwise raise.

        :raises ProjectionBudgetError: if ``row_count > max_rows``
        """
        _validate_max_rows(max_rows)
        if self.row_count > max_rows:
            raise ProjectionBudgetError(self.row_count, max_rows, self)
        return self

    def describe(self) -> str:
        """Return a compact human-readable description of the plan."""
        column_text = ", ".join(
            f"{column.name} <- {column.path} ({column.occurrences})" for column in self.columns
        )
        return (
            f"{self.row_count} rows [{self.mode.value}]: {column_text}"
            if column_text
            else f"{self.row_count} rows [{self.mode.value}]: <no columns>"
        )

    def __repr__(self) -> str:
        return (
            f"ProjectionPlan(row_count={self.row_count}, mode={self.mode.value!r}, "
            f"columns={self.column_names!r})"
        )


@dataclass
class ProjectionResult:
    """The complete output of :meth:`Projection.project`.

    Rows are materialized in full before this object is returned, so a caller
    never observes a partially expanded record set.
    """

    plan: ProjectionPlan
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def records(self) -> list[dict[str, Any]]:
        """Alias for :attr:`rows`."""
        return self.rows

    @property
    def columns(self) -> tuple[str, ...]:
        return self.plan.column_names

    @property
    def sources(self) -> dict[str, str]:
        return self.plan.sources

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)


@dataclass
class _ColumnStats:
    missing: int = 0
    empty: int = 0
    tags: int = 0


def _validate_max_rows(max_rows: int) -> None:
    if not isinstance(max_rows, int) or isinstance(max_rows, bool):
        raise TypeError("max_rows must be an integer")
    if max_rows < 0:
        raise ValueError("max_rows cannot be negative")


def _array_items(value: Any) -> list[Any] | None:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: (type(item).__name__, repr(item)))
    return None


def _display_path(parts: list[str]) -> str:
    text = "".join(parts)
    if text.startswith("."):
        text = text[1:]
    return text or "$"


def _path_of(column: Column) -> Path:
    path = column.path
    assert isinstance(path, Path)
    return path


def _key_fragment(key: Any) -> str:
    if isinstance(key, str) and _IDENTIFIER_RE.match(key):
        return f".{key}"
    return f"[{key!r}]"


@dataclass
class _TrieNode:
    """A node in the shared prefix tree of projection paths."""

    children: dict[tuple[str, Any], _TrieNode] = field(default_factory=dict)
    leaves: list[Column] = field(default_factory=list)

    def add(self, column: Column) -> None:
        node = self
        for step in _path_of(column).steps:
            node = node.children.setdefault((step.kind, step.value), _TrieNode())
        node.leaves.append(column)

    def branch_columns(self, key: tuple[str, Any]) -> list[Column]:
        return _all_columns(self.children[key])

    def descendant_columns(self) -> list[Column]:
        columns: list[Column] = []
        for child in self.children.values():
            columns.extend(_all_columns(child))
        return columns


def _all_columns(node: _TrieNode) -> list[Column]:
    columns = list(node.leaves)
    for child in node.children.values():
        columns.extend(_all_columns(child))
    return columns


class _Evaluation:
    """A count during planning, or fully materialized rows during projection."""

    __slots__ = ("count", "rows", "expanded")

    def __init__(
        self,
        count: int,
        rows: list[dict[str, Any]] | None = None,
        expanded: bool = False,
    ) -> None:
        self.count = count
        self.rows = rows
        self.expanded = expanded


class _Evaluator:
    """Evaluate a projection trie while collecting provenance statistics.

    With ``collect=False`` only counts are produced, so planning is
    proportional to the input shape (plus the final row count) rather than to
    the output size.  With ``collect=True`` the same traversal also builds the
    records.
    """

    def __init__(
        self,
        root: _TrieNode,
        mode: ExpansionMode,
        policy: ProjectionPolicy,
        *,
        collect: bool,
    ) -> None:
        self.root = root
        self.mode = mode
        self.policy = policy
        self.collect = collect
        self.stats: dict[str, _ColumnStats] = {}
        self.occurrences: dict[str, int] = {}
        self._column_policy: dict[str, ProjectionPolicy] = {}

    def evaluate(self, value: Any) -> _Evaluation:
        return self._eval_node(self.root, value, [], {})

    def _policy_for(self, column: Column) -> ProjectionPolicy:
        cached = self._column_policy.get(column.name)
        if cached is not None:
            return cached

        if column.missing is None and column.empty is None and column.tag is None:
            result = self.policy
        else:
            result = ProjectionPolicy(
                missing=column.missing if column.missing is not None else self.policy.missing,
                empty=column.empty if column.empty is not None else self.policy.empty,
                tag=column.tag if column.tag is not None else self.policy.tag,
                missing_value=self.policy.missing_value,
                tag_resolver=self.policy.tag_resolver,
                auto_expand=self.policy.auto_expand,
            )
        self._column_policy[column.name] = result
        return result

    def _stat(self, column: Column) -> _ColumnStats:
        return self.stats.setdefault(column.name, _ColumnStats())

    def _record_occurrence(self, column: Column) -> None:
        self.occurrences[column.name] = self.occurrences.get(column.name, 0) + 1

    def _assemble_plan(self, row_count: int, columns: tuple[Column, ...]) -> ProjectionPlan:
        sources = tuple(
            ColumnSource(
                name=column.name,
                path=str(_path_of(column)),
                segments=_path_of(column).steps,
                expands=_path_of(column).expands,
                occurrences=self.occurrences.get(column.name, 0),
                missing=self._stat(column).missing,
                empty=self._stat(column).empty,
                tags=self._stat(column).tags,
            )
            for column in columns
        )
        return ProjectionPlan(
            columns=sources,
            row_count=row_count,
            mode=self.mode,
            policy=self.policy,
        )

    def _rows(
        self,
        count: int,
        factory: Callable[[], list[dict[str, Any]]],
        *,
        expanded: bool = False,
    ) -> _Evaluation:
        if self.collect:
            return _Evaluation(count, factory(), expanded)
        return _Evaluation(count, None, expanded)

    def _eval_node(
        self,
        node: _TrieNode,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        base: dict[str, Any] = {}
        for column in node.leaves:
            base[column.name] = value
            self._record_occurrence(column)

        if not node.children:
            if not node.leaves:
                return _Evaluation(0)
            return self._rows(1, lambda: [{**base}])

        if isinstance(value, _tag_type()):
            result = self._eval_tag(node, cast("CBORTag", value), parts, active)
        else:
            result = self._eval_children(node, value, parts, active)

        if base and self.collect and result.rows is not None:
            result.rows = [{**base, **row} for row in result.rows]
        return result

    def _eval_children(
        self,
        node: _TrieNode,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        keys = list(node.children)
        results = [
            self._eval_step(node.children[key], PathStep(key[0], key[1]), value, parts, active)
            for key in keys
        ]
        if not results:
            return _Evaluation(0)
        return self._merge_branches(node, keys, results)

    def _merge_branches(
        self,
        node: _TrieNode,
        keys: list[tuple[str, Any]],
        results: list[_Evaluation],
    ) -> _Evaluation:
        column_sets = [node.branch_columns(key) for key in keys]
        merged = results[0]
        merged_columns = column_sets[0]
        for index, other in enumerate(results[1:], start=1):
            merged = self._combine(merged, other, merged_columns, column_sets[index])
            merged_columns = merged_columns + column_sets[index]
        return merged

    def _combine(
        self,
        left: _Evaluation,
        right: _Evaluation,
        left_columns: list[Column],
        right_columns: list[Column],
    ) -> _Evaluation:
        if self.mode is ExpansionMode.CROSS:
            count = left.count * right.count
            left_rows = left.rows or []
            right_rows = right.rows or []
            return self._rows(
                count,
                lambda: [{**a, **b} for a in left_rows for b in right_rows],
                expanded=left.expanded or right.expanded,
            )

        left_scalar = not left.expanded and left.count == 1
        right_scalar = not right.expanded and right.count == 1
        left_occurrences = 1 if left_scalar else left.count
        right_occurrences = 1 if right_scalar else right.count

        if left.count == 0 or right.count == 0:
            return _Evaluation(0)

        missing_modes = {self._policy_for(column).missing for column in right_columns}
        if len(missing_modes) > 1:
            missing_modes = {self.policy.missing}

        if left_occurrences != right_occurrences and MissingPolicy.ERROR in missing_modes:
            raise ProjectionCardinalityError(
                "paired projection branches have different occurrence counts "
                f"({left_occurrences} and {right_occurrences}) and on_missing='error'"
            )

        if MissingPolicy.SKIP in missing_modes:
            row_count = min(left_occurrences, right_occurrences)
        else:
            row_count = max(left_occurrences, right_occurrences)

        def build() -> list[dict[str, Any]]:
            assert left.rows is not None and right.rows is not None
            left_rows = left.rows
            right_rows = right.rows
            fill = self.policy.missing_value
            rows: list[dict[str, Any]] = []
            for index in range(row_count):
                row: dict[str, Any] = {}
                if left_scalar:
                    row.update(left_rows[0])
                elif index < len(left_rows):
                    row.update(left_rows[index])
                else:
                    for column in left_columns:
                        row.setdefault(column.name, fill)

                if right_scalar:
                    row.update(right_rows[0])
                elif index < len(right_rows):
                    row.update(right_rows[index])
                else:
                    for column in right_columns:
                        row.setdefault(column.name, fill)
                rows.append(row)
            return rows

        return self._rows(row_count, build, expanded=left.expanded or right.expanded)

    def _eval_step(
        self,
        node: _TrieNode,
        step: PathStep,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        if step.kind == "key":
            return self._eval_key(node, step.value, value, parts, active)
        if step.kind == "index":
            return self._eval_index(node, step.value, value, parts, active)
        if step.kind == "expand":
            return self._eval_expand(node, value, parts, active)
        return self._eval_recursive(node, value, parts, active)

    def _eval_key(
        self,
        node: _TrieNode,
        key: Any,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        if isinstance(value, Mapping):
            if key in value:
                return self._eval_node(node, value[key], parts + [_key_fragment(key)], active)
            return self._missing_branch(node, parts + [_key_fragment(key)])

        items = _array_items(value)
        if items is not None and self.policy.auto_expand:
            return self._eval_auto_expand(node, key, value, items, parts, active)

        return self._missing_branch(node, parts + [_key_fragment(key)])

    def _eval_auto_expand(
        self,
        node: _TrieNode,
        key: Any,
        value: Any,
        items: list[Any],
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        if not items:
            return self._empty_branch(node, parts)

        path_text = _display_path(parts)
        value_id = id(value)
        if value_id in active:
            raise ProjectionCycleError(path_text, active[value_id], value)

        active[value_id] = path_text
        try:
            results = [
                self._eval_key(node, key, item, parts + [f"[{item_index}]"], active)
                for item_index, item in enumerate(items)
            ]
            return self._concatenate(results)
        finally:
            del active[value_id]

    def _eval_index(
        self,
        node: _TrieNode,
        index: int,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        items = _array_items(value)
        fragment = f"[{index}]"
        if items is not None and 0 <= index < len(items):
            return self._eval_node(node, items[index], parts + [fragment], active)
        return self._missing_branch(node, parts + [fragment])

    def _eval_expand(
        self,
        node: _TrieNode,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        items = _array_items(value)
        if items is None:
            # Expanding a non-array is one occurrence; the remainder of the
            # path is evaluated against the same value.
            return self._eval_node(node, value, parts, active)

        if not items:
            return self._empty_branch(node, parts)

        results = [
            self._eval_node(node, item, parts + [f"[{item_index}]"], active)
            for item_index, item in enumerate(items)
        ]
        return self._concatenate(results)

    def _eval_recursive(
        self,
        node: _TrieNode,
        value: Any,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        items = _array_items(value)
        if items is None:
            return self._eval_node(node, value, parts, active)

        if not items:
            return self._empty_branch(node, parts)

        path_text = _display_path(parts)
        value_id = id(value)
        if value_id in active:
            raise ProjectionCycleError(path_text, active[value_id], value)

        active[value_id] = path_text
        try:
            results = [
                self._eval_recursive(node, item, parts + [f"[{item_index}]"], active)
                for item_index, item in enumerate(items)
            ]
            return self._concatenate(results)
        finally:
            del active[value_id]

    def _concatenate(self, results: list[_Evaluation]) -> _Evaluation:
        count = sum(result.count for result in results)
        if not self.collect:
            return _Evaluation(count, None, True)

        rows: list[dict[str, Any]] = []
        for result in results:
            assert result.rows is not None
            rows.extend(result.rows)
        return _Evaluation(count, rows, True)

    def _eval_tag(
        self,
        node: _TrieNode,
        tag: CBORTag,
        parts: list[str],
        active: dict[int, str],
    ) -> _Evaluation:
        columns = node.descendant_columns()
        policies = {self._policy_for(column).tag for column in columns}
        policy = policies.pop() if len(policies) == 1 else self.policy.tag

        resolved: Any = _UNSET
        resolution_error: Exception | None = None
        if self.policy.tag_resolver is not None:
            try:
                candidate = self.policy.tag_resolver(tag)
            except Exception as exc:
                resolution_error = exc
            else:
                if candidate is not tag:
                    resolved = candidate

        if resolved is _UNSET and policy is TagPolicy.UNWRAP:
            resolved = tag.value

        if resolved is not _UNSET:
            return self._eval_children(node, resolved, parts, active)

        path_text = _display_path(parts)
        if policy is TagPolicy.OPAQUE:
            for column in columns:
                self._stat(column).tags += 1
                self._record_occurrence(column)

            def build() -> list[dict[str, Any]]:
                return [{column.name: tag for column in columns}]

            return self._rows(1, build)

        if policy is TagPolicy.ZERO:
            for column in columns:
                self._stat(column).tags += 1
            return _Evaluation(0)

        detail = f": {resolution_error}" if resolution_error is not None else ""
        raise ProjectionTagError(
            f"unresolvable tag {tag.tag} at projection path {path_text!r}{detail}"
        ) from resolution_error

    def _missing_branch(self, node: _TrieNode, parts: list[str]) -> _Evaluation:
        columns = _all_columns(node)
        if not columns:
            return _Evaluation(0)

        policies = [self._policy_for(column) for column in columns]
        for column, policy in zip(columns, policies):
            self._stat(column).missing += 1
            if policy.missing is MissingPolicy.ERROR:
                raise ProjectionCardinalityError(
                    f"missing value at projection path {_display_path(parts)!r} "
                    f"(column {column.name!r})"
                )

        null_columns = [
            column
            for column, policy in zip(columns, policies)
            if policy.missing is MissingPolicy.NULL
        ]
        if not null_columns:
            return _Evaluation(0, None, True)

        for column in null_columns:
            self._record_occurrence(column)

        def build() -> list[dict[str, Any]]:
            fill = self.policy.missing_value
            return [{column.name: fill for column in null_columns}]

        return self._rows(1, build)

    def _empty_branch(self, node: _TrieNode, parts: list[str]) -> _Evaluation:
        columns = _all_columns(node)
        if not columns:
            return _Evaluation(0)

        policies = [self._policy_for(column) for column in columns]
        for column, policy in zip(columns, policies):
            self._stat(column).empty += 1
            if policy.empty is EmptyPolicy.ERROR:
                raise ProjectionCardinalityError(
                    f"empty array at projection path {_display_path(parts)!r} "
                    f"(column {column.name!r})"
                )

        null_columns = [
            column for column, policy in zip(columns, policies) if policy.empty is EmptyPolicy.NULL
        ]
        if not null_columns:
            return _Evaluation(0, None, True)

        for column in null_columns:
            self._record_occurrence(column)

        def build() -> list[dict[str, Any]]:
            fill = self.policy.missing_value
            return [{column.name: fill for column in null_columns}]

        return self._rows(1, build)


class Projection:
    """A row-oriented projection of decoded CBOR values.

    Columns can be supplied as :class:`Column` instances, as ``(name, path)``
    pairs, as plain strings (where the string is both name and path), or as a
    mapping of names to paths::

        projection = Projection({
            "order": "orders[].id",
            "sku": "orders[].items[].sku",
        }, mode="cross")

        plan = projection.plan(obj)
        print(plan.row_count, plan.describe())

        result = projection.project(obj, max_rows=1000)
        for row in result:
            ...

    The projection never modifies the decoded value.
    """

    def __init__(
        self,
        columns: Iterable[Column | str | tuple[str, Any]] | Mapping[str, Any],
        *,
        mode: ExpansionMode | str = ExpansionMode.PAIRED,
        policy: ProjectionPolicy | None = None,
        on_missing: MissingPolicy | str | None = None,
        on_empty: EmptyPolicy | str | None = None,
        on_tag: TagPolicy | str | None = None,
        missing_value: Any = _UNSET,
        tag_resolver: Callable[[CBORTag], Any] | None = _UNSET,  # type: ignore[assignment]
        auto_expand: bool | None = None,
    ) -> None:
        if policy is not None and not isinstance(policy, ProjectionPolicy):
            raise TypeError("policy must be a ProjectionPolicy instance")

        base = policy or ProjectionPolicy()
        overrides: dict[str, Any] = {}
        if on_missing is not None:
            overrides["missing"] = on_missing
        if on_empty is not None:
            overrides["empty"] = on_empty
        if on_tag is not None:
            overrides["tag"] = on_tag
        if missing_value is not _UNSET:
            overrides["missing_value"] = missing_value
        if tag_resolver is not _UNSET:
            overrides["tag_resolver"] = tag_resolver
        if auto_expand is not None:
            overrides["auto_expand"] = auto_expand

        self.policy = (
            ProjectionPolicy(
                missing=overrides.get("missing", base.missing),
                empty=overrides.get("empty", base.empty),
                tag=overrides.get("tag", base.tag),
                missing_value=overrides.get("missing_value", base.missing_value),
                tag_resolver=overrides.get("tag_resolver", base.tag_resolver),
                auto_expand=overrides.get("auto_expand", base.auto_expand),
            )
            if overrides
            else base
        )
        self.mode = _coerce_enum(ExpansionMode, mode, "mode")

        if isinstance(columns, Mapping):
            raw_columns: Iterable[Any] = columns.items()
        else:
            raw_columns = columns

        normalized: list[Column] = []
        for item in raw_columns:
            if isinstance(item, Column):
                normalized.append(item)
            elif isinstance(item, str):
                normalized.append(Column(item, item))
            elif isinstance(item, tuple) and len(item) == 2:
                normalized.append(Column(item[0], item[1]))
            else:
                raise TypeError(
                    "columns must be Column instances, (name, path) pairs, strings "
                    "or a mapping of names to paths"
                )

        names = [column.name for column in normalized]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate column names: {', '.join(duplicates)}")

        self.columns = tuple(normalized)
        self._root = _TrieNode()
        for column in self.columns:
            self._root.add(column)

    def __repr__(self) -> str:
        return (
            f"Projection(columns={[column.name for column in self.columns]!r}, "
            f"mode={self.mode.value!r})"
        )

    def _evaluate(
        self, value: Any, *, collect: bool
    ) -> tuple[ProjectionPlan, list[dict[str, Any]]]:
        evaluator = _Evaluator(self._root, self.mode, self.policy, collect=collect)
        result = evaluator.evaluate(value)
        plan = evaluator._assemble_plan(result.count, self.columns)
        return plan, result.rows or []

    def plan(
        self, value: Any, *, max_rows: int | None = None, budget: int | None = None
    ) -> ProjectionPlan:
        """Compute the exact row count and column sources without records.

        :param max_rows: optional row budget; ``budget`` is an alias
        :raises ProjectionCycleError: if recursion follows a cycle
        :raises ProjectionBudgetError: if a budget is given and the plan does
            not fit it
        """
        if budget is not None:
            if max_rows is not None and budget != max_rows:
                raise ValueError("max_rows and budget specify different limits")
            max_rows = budget

        plan, _ = self._evaluate(value, collect=False)
        if max_rows is not None:
            plan.ensure_within(max_rows)
        return plan

    def project(
        self, value: Any, *, max_rows: int | None = None, budget: int | None = None
    ) -> ProjectionResult:
        """Generate all records, subject to an optional exact row budget.

        The exact plan is computed first, without building records.  If
        ``max_rows`` is given and the row count exceeds it,
        :class:`ProjectionBudgetError` is raised before any record is built.
        The result is then fully materialized, so a failure while producing a
        row also leaves no partial result.  ``budget`` is an alias for
        ``max_rows``.
        """
        if budget is not None:
            if max_rows is not None and budget != max_rows:
                raise ValueError("max_rows and budget specify different limits")
            max_rows = budget

        if max_rows is not None:
            _validate_max_rows(max_rows)

        plan, _ = self._evaluate(value, collect=False)
        if max_rows is not None and plan.row_count > max_rows:
            raise ProjectionBudgetError(plan.row_count, max_rows, plan)

        _, rows = self._evaluate(value, collect=True)
        if len(rows) != plan.row_count:
            raise ProjectionError(
                f"projection produced {len(rows)} rows but its plan promised {plan.row_count}"
            )

        return ProjectionResult(plan=plan, rows=rows)

    def rows(
        self, value: Any, *, max_rows: int | None = None, budget: int | None = None
    ) -> list[dict[str, Any]]:
        """Convenience wrapper returning only the record list."""
        return self.project(value, max_rows=max_rows, budget=budget).rows

    def plan_with_budget(self, value: Any, max_rows: int) -> ProjectionPlan:
        """Return the plan only if it fits ``max_rows``.

        This is an alias for ``plan(value, max_rows=max_rows)``.
        """
        return self.plan(value, max_rows=max_rows)


def plan_projection(
    value: Any,
    columns: Iterable[Column | str | tuple[str, Any]] | Mapping[str, Any],
    *,
    mode: ExpansionMode | str = ExpansionMode.PAIRED,
    policy: ProjectionPolicy | None = None,
    on_missing: MissingPolicy | str | None = None,
    on_empty: EmptyPolicy | str | None = None,
    on_tag: TagPolicy | str | None = None,
    missing_value: Any = _UNSET,
    tag_resolver: Callable[[CBORTag], Any] | None = _UNSET,  # type: ignore[assignment]
    auto_expand: bool | None = None,
    max_rows: int | None = None,
    budget: int | None = None,
) -> ProjectionPlan:
    """Build a projection and return its exact plan."""
    projection = Projection(
        columns,
        mode=mode,
        policy=policy,
        on_missing=on_missing,
        on_empty=on_empty,
        on_tag=on_tag,
        missing_value=missing_value,
        tag_resolver=tag_resolver,
        auto_expand=auto_expand,
    )
    return projection.plan(value, max_rows=max_rows, budget=budget)


def project(
    value: Any,
    columns: Iterable[Column | str | tuple[str, Any]] | Mapping[str, Any],
    *,
    mode: ExpansionMode | str = ExpansionMode.PAIRED,
    policy: ProjectionPolicy | None = None,
    on_missing: MissingPolicy | str | None = None,
    on_empty: EmptyPolicy | str | None = None,
    on_tag: TagPolicy | str | None = None,
    missing_value: Any = _UNSET,
    tag_resolver: Callable[[CBORTag], Any] | None = _UNSET,  # type: ignore[assignment]
    auto_expand: bool | None = None,
    max_rows: int | None = None,
    budget: int | None = None,
) -> ProjectionResult:
    """Project a decoded CBOR value into complete records."""
    projection = Projection(
        columns,
        mode=mode,
        policy=policy,
        on_missing=on_missing,
        on_empty=on_empty,
        on_tag=on_tag,
        missing_value=missing_value,
        tag_resolver=tag_resolver,
        auto_expand=auto_expand,
    )
    return projection.project(value, max_rows=max_rows, budget=budget)


def project_rows(
    value: Any,
    columns: Iterable[Column | str | tuple[str, Any]] | Mapping[str, Any],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Project a decoded CBOR value and return only the record list."""
    return project(value, columns, **kwargs).rows
