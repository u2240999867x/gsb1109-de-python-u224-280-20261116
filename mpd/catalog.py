"""Reconstruction of grouped MPD ``list`` responses.

``MPDClient.list`` can group tag values::

    list album group albumartist group date

MPD answers with a flat stream of ``Tag: value`` lines.  Upper-level fields
are not necessarily repeated on every line; a group value is declared once and
then implicitly shared by the following lines until that group changes.

Grouping is a property of the request.  In MPD's response the groups are
emitted in reverse request order: the last requested group is the outermost
level, the first requested group is the innermost one, and the listed tag
follows below them.  A declaration of an outer group resets every inner level
at that protocol boundary, so a value left over from the previous group is
never borrowed into the next one.  Two requests with different group orders
consequently have different column mappings; no implicit field positions are
shared between parser instances.

The parser distinguishes three cases that are easy to conflate:

* a group level that was never declared in the current context (``missing``),
* a field that was declared with an empty value (explicit ``""``),
* a response that ended before MPD's terminating ``OK`` (``truncated``).

Every row is backed by an actual leaf line of the response, and every
occurrence of a multi-value tag is kept.  The reconstruction never forms the
Cartesian product of per-level unique values.

Use :func:`parse_grouped_list` for already collected lines, or
:class:`GroupedListParser` for incrementally arriving ones.
"""

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, Union


class CatalogParseError(ValueError):
    """Raised when a response line is not a ``Tag: value`` pair."""


class CatalogTruncatedError(Exception):
    """Raised when the response ends before the terminating ``OK``.

    The partially reconstructed result is available as :attr:`result`.
    """

    def __init__(self, message: str, result: "CatalogResult") -> None:
        super().__init__(message)
        self.result = result


class FieldSource(str, Enum):
    """Where a value in a catalog row came from."""

    DECLARED = "declared"
    INHERITED = "inherited"


@dataclass(frozen=True)
class FieldOccurrence:
    """One ``Tag: value`` occurrence in a response.

    ``line`` is the zero-based index of the response line that declared this
    value.  An inherited value keeps the line number of the declaration it
    reuses, so the chain back to the original declaration stays visible.
    """

    key: str
    value: str
    source: FieldSource
    line: int

    @property
    def declared(self) -> bool:
        return self.source is FieldSource.DECLARED

    @property
    def empty(self) -> bool:
        return self.value == ""


@dataclass
class CatalogRow:
    """One catalog row, backed by a single leaf line of the response.

    ``groups`` holds every occurrence for each group key.  A group value is
    ``DECLARED`` when its declaration belongs to this row's block (the lines
    since the previous leaf line) and ``INHERITED`` when it comes from an
    earlier declaration, in which case ``line`` points at it.  Repeated group
    declarations in one block are kept as a multi-value tag.

    ``missing`` lists group levels that were never declared in this context;
    it is distinct from a group whose value is explicitly ``""``.  ``extra``
    keeps other tag occurrences that accompanied this row.
    """

    index: int
    line: int
    leaf: FieldOccurrence
    groups: Dict[str, Tuple[FieldOccurrence, ...]]
    missing: Tuple[str, ...]
    truncated: bool = False
    extra: Dict[str, Tuple[FieldOccurrence, ...]] = dataclass_field(
        default_factory=dict
    )

    @property
    def supported(self) -> bool:
        return not self.missing

    @property
    def complete(self) -> bool:
        return not self.missing and not self.truncated

    def occurrences(self, key: str) -> Tuple[FieldOccurrence, ...]:
        key = key.lower()
        if key == self.leaf.key:
            return (self.leaf,)
        if key in self.groups:
            return self.groups[key]
        return self.extra.get(key, ())

    def provenance(self, key: str) -> FieldOccurrence:
        """Return the first occurrence for ``key`` or raise :class:`KeyError`.

        Missing levels are reported through :attr:`missing`; they are never
        filled in from another row.  Use :meth:`occurrences` for multi-value
        tags.
        """

        occurrences = self.occurrences(key)
        if not occurrences:
            raise KeyError(key)
        return occurrences[0]

    def is_missing(self, key: str) -> bool:
        return key.lower() in self.missing

    def is_empty(self, key: str) -> bool:
        occurrences = self.occurrences(key)
        return bool(occurrences) and occurrences[0].empty

    def as_dict(self) -> Dict[str, Any]:
        """Return the row as a plain mapping.

        Missing group levels are omitted, never filled from another row.
        Explicitly empty values stay present as ``""``.  Multi-value tags keep
        all occurrences as a list.
        """

        result: Dict[str, Any] = {}
        for key, occurrences in self.groups.items():
            values = [occurrence.value for occurrence in occurrences]
            result[key] = values[0] if len(values) == 1 else values
        result[self.leaf.key] = self.leaf.value
        for key, occurrences in self.extra.items():
            values = [occurrence.value for occurrence in occurrences]
            result[key] = values[0] if len(values) == 1 else values
        return result


@dataclass
class CatalogResult:
    """The reconstructed result of one grouped ``list`` request."""

    tag: str
    groups: Tuple[str, ...]
    rows: List[CatalogRow] = dataclass_field(default_factory=list)
    complete: bool = True
    pending: Optional[Dict[str, FieldOccurrence]] = None
    error: Optional[str] = None

    @property
    def truncated(self) -> bool:
        return not self.complete

    @property
    def incomplete_rows(self) -> List[CatalogRow]:
        return [row for row in self.rows if not row.complete]

    @property
    def unsupported_rows(self) -> List[CatalogRow]:
        return [row for row in self.rows if not row.supported]

    def combinations(self) -> Iterator[Dict[str, Any]]:
        """Yield one mapping per actual leaf line in the response.

        A missing group level stays absent; it is never completed with a
        value taken from another row, so no invented entry is produced.  The
        gap remains visible through ``row.missing``.  An explicitly empty
        group value is present as ``""``.  Rows without a leaf line (only
        possible for a truncated response) produce nothing.
        """

        for row in self.rows:
            yield row.as_dict()


def normalize_groups(groups: Union[str, Iterable[str]]) -> Tuple[str, ...]:
    """Return group names in request order, validated and lower-cased."""

    if isinstance(groups, str):
        groups = (groups,)
    normalized: List[str] = []
    seen = set()
    for group in groups:
        if not isinstance(group, str) or not group:
            raise ValueError("group tags must be non-empty strings")
        key = group.lower()
        if key in seen:
            raise ValueError("duplicate group tag: {!r}".format(group))
        seen.add(key)
        normalized.append(key)
    return tuple(normalized)


def split_list_args(args: Iterable[Any]) -> Tuple[str, List[Any], Tuple[str, ...]]:
    """Split ``list`` command arguments into tag, filters and group order."""

    items = list(args)
    if not items:
        raise ValueError("list requires a tag")
    tag = items[0]
    if not isinstance(tag, str) or not tag:
        raise ValueError("list tag must be a non-empty string")

    filters: List[Any] = []
    groups: List[str] = []
    index = 1
    while index < len(items):
        item = items[index]
        if isinstance(item, str) and item.lower() == "group":
            if index + 1 >= len(items):
                raise ValueError("'group' must be followed by a tag")
            groups.append(items[index + 1])
            index += 2
        else:
            if index + 1 >= len(items):
                raise ValueError(
                    "filters must be (type, what) pairs; got trailing {!r}".format(item)
                )
            filters.extend(items[index : index + 2])
            index += 2
    return tag, filters, normalize_groups(groups)


def build_catalog_args(
    tag: str,
    filters: Iterable[Any] = (),
    groups: Union[str, Iterable[str]] = (),
) -> Tuple[List[Any], Tuple[str, ...]]:
    """Build MPD arguments and the normalized request group order."""

    if not isinstance(tag, str) or not tag:
        raise ValueError("list tag must be a non-empty string")
    filter_args = tuple(filters)
    if len(filter_args) % 2:
        raise ValueError("filters must be (type, what) pairs")
    group_order = normalize_groups(groups)
    if tag.lower() in group_order:
        raise ValueError("the listed tag cannot also be a group")
    args: List[Any] = [tag]
    args.extend(filter_args)
    for group in group_order:
        args.extend(("group", group))
    return args, group_order


class GroupedListParser:
    """Incrementally parse a grouped ``list`` response.

    ``groups`` is this request's group order, outermost first.  Each parser
    instance derives its response column mapping from that order, so
    alternating requests with different group orders cannot reuse implicit
    field positions from one another.
    """

    def __init__(self, tag: str, groups: Union[str, Iterable[str]] = ()) -> None:
        if not isinstance(tag, str) or not tag:
            raise ValueError("list tag must be a non-empty string")
        self._tag = tag.lower()
        self._groups = normalize_groups(groups)
        if self._tag in self._groups:
            raise ValueError("the listed tag cannot also be a group")
        # MPD prints the last requested group first (outermost) and the first
        # requested group last (innermost).  This mapping belongs to this
        # request only.
        self._columns = tuple(reversed(self._groups))
        self._levels = {group: level for level, group in enumerate(self._columns)}
        self._context: Dict[int, List[FieldOccurrence]] = {}
        self._aux: Dict[str, List[FieldOccurrence]] = {}
        self._last_leaf = -1
        self._line = -1
        self._rows: List[CatalogRow] = []
        self._finished = False
        self._result: Optional[CatalogResult] = None

    @property
    def tag(self) -> str:
        return self._tag

    @property
    def groups(self) -> Tuple[str, ...]:
        """The request group order, outermost first."""

        return self._groups

    def feed(self, line: str) -> None:
        """Consume one response line (without the trailing newline)."""

        if self._finished:
            raise RuntimeError("cannot feed lines after finish()")
        if line.endswith("\n"):
            line = line[:-1]
        key, separator, value = line.partition(": ")
        if not separator:
            raise CatalogParseError("Could not parse pair: {!r}".format(line))
        key = key.lower()
        self._line += 1
        line_number = self._line

        level = self._levels.get(key)
        if level is not None:
            # Entering a group clears every inner level at the protocol
            # boundary.  A value from the previous branch must not survive
            # into the new one, and auxiliary values that were never claimed
            # by a leaf line belong to the closed branch.
            for deeper in [depth for depth in self._context if depth > level]:
                del self._context[deeper]
            self._aux = {}
            occurrence = FieldOccurrence(key, value, FieldSource.DECLARED, line_number)
            # Repeated declarations at the same level form a multi-value
            # group tag and are kept in order.
            current = self._context.get(level)
            if current and current[-1].line > self._last_leaf:
                current.append(occurrence)
            else:
                self._context[level] = [occurrence]
            return

        if key == self._tag:
            self._emit_leaf(
                FieldOccurrence(key, value, FieldSource.DECLARED, line_number)
            )
            return

        self._aux.setdefault(key, []).append(
            FieldOccurrence(key, value, FieldSource.DECLARED, line_number)
        )

    def finish(self, complete: bool = True) -> CatalogResult:
        """Finish parsing and return the reconstructed result.

        Pass ``complete=False`` when the line stream ended before MPD sent its
        ``OK`` terminator.  Rows already backed by received leaf lines are
        returned, marked as truncated.  A trailing group context without leaf
        lines is exposed as :attr:`CatalogResult.pending` instead of being
        turned into a catalog row.
        """

        if self._finished:
            assert self._result is not None
            return self._result

        if not complete:
            for row in self._rows:
                row.truncated = True

        pending: Optional[Dict[str, FieldOccurrence]] = None
        new_context = [
            occurrences[-1]
            for _, occurrences in sorted(self._context.items())
            if occurrences[-1].line > self._last_leaf
        ]
        if not complete and (new_context or self._aux):
            pending = {occurrence.key: occurrence for occurrence in new_context}
            for key, occurrences in self._aux.items():
                pending[key] = occurrences[-1]
        self._finished = True
        self._result = CatalogResult(
            tag=self._tag,
            groups=self._groups,
            rows=self._rows,
            complete=complete,
            pending=pending,
            error=None if complete else "response truncated before OK",
        )
        return self._result

    def _emit_leaf(self, leaf: FieldOccurrence) -> None:
        groups: Dict[str, Tuple[FieldOccurrence, ...]] = {}
        for group in self._groups:
            occurrences = self._context.get(self._levels[group])
            if occurrences is None:
                continue
            converted = []
            for occurrence in occurrences:
                source = (
                    FieldSource.DECLARED
                    if occurrence.line > self._last_leaf
                    else FieldSource.INHERITED
                )
                converted.append(
                    FieldOccurrence(group, occurrence.value, source, occurrence.line)
                )
            groups[group] = tuple(converted)
        missing = tuple(
            group for group in self._groups if self._levels[group] not in self._context
        )
        extra = {key: tuple(values) for key, values in self._aux.items()}
        self._rows.append(
            CatalogRow(
                index=len(self._rows),
                line=leaf.line,
                leaf=leaf,
                groups=groups,
                missing=missing,
                extra=extra,
            )
        )
        self._last_leaf = leaf.line
        self._aux = {}


def parse_grouped_list(
    lines: Iterable[str],
    tag: str,
    groups: Union[str, Iterable[str]] = (),
    complete: bool = True,
) -> CatalogResult:
    """Parse an iterable of grouped ``list`` response lines.

    ``groups`` is the request group order, outermost first.
    """

    parser = GroupedListParser(tag, groups)
    for line in lines:
        parser.feed(line)
    return parser.finish(complete=complete)


def catalog_combinations(result: CatalogResult) -> Iterator[Mapping[str, Any]]:
    """Convenience wrapper returning only response-backed combinations."""

    return result.combinations()
