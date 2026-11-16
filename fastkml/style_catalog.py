"""
Resolve effective KML styles for every feature in a document tree.

KML styles can be layered in several ways: a ``Feature`` may point at a shared
``Style`` or ``StyleMap`` with ``styleUrl``, it may carry inline ``Style`` or
``StyleMap`` children, and a ``StyleMap`` may itself point at another selector
(including a nested ``StyleMap``). :class:`StyleCatalog` walks such a tree once
and records, for both the ``normal`` and ``highlight`` state, which declaration
wins for every individual style attribute.

Resolution is deliberately read-only. Building a catalog, resolving the
highlight state, or materialising a :class:`Style` with
:meth:`ResolvedStyle.to_style` never mutates the parsed declarations or
replaces them with effective values. Use one catalog (or rebuild it) after
editing the object tree; a catalog is a snapshot of the tree it was built from.
"""

from __future__ import annotations

import copy
import logging
import urllib.parse as urlparse
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

from fastkml.base import _XMLObject
from fastkml.enums import PairKey
from fastkml.styles import BalloonStyle
from fastkml.styles import HotSpot
from fastkml.styles import Icon
from fastkml.styles import IconStyle
from fastkml.styles import LabelStyle
from fastkml.styles import LineStyle
from fastkml.styles import PolyStyle
from fastkml.styles import Style
from fastkml.styles import StyleMap
from fastkml.styles import StyleUrl
from fastkml.styles import _StyleSelector

if TYPE_CHECKING:
    from fastkml.features import _Feature

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalStyleReference",
    "FeatureStyleEntry",
    "FeatureStyles",
    "MapStyleCatalog",
    "MissingStyleTarget",
    "ResolvedStyle",
    "StyleCatalog",
    "StyleCycle",
    "StyleDeclaration",
    "StyleReferenceIssue",
    "StyleResolver",
    "StyleSource",
    "build_map_catalog",
    "build_style_catalog",
    "catalog_styles",
    "get_style_catalog",
    "resolve_feature_styles",
    "resolve_style",
    "resolve_styles",
]

_SUB_STYLE_TYPES: Final = (
    IconStyle,
    LabelStyle,
    LineStyle,
    PolyStyle,
    BalloonStyle,
)

# Attributes inherited from ``_XMLObject``/``_BaseObject`` identify the XML
# element, not its visual state, so they never take part in style merging.
_IDENTITY_ATTRIBUTES: Final = frozenset({"id", "target_id", "ns", "name_spaces"})


def _coerce_mode(mode: PairKey | str) -> PairKey:
    if isinstance(mode, PairKey):
        return mode
    try:
        return PairKey(mode)
    except ValueError as exc:
        msg = f"Unknown style mode {mode!r}; expected 'normal' or 'highlight'"
        raise ValueError(msg) from exc


def _selector_label(selector: _StyleSelector) -> str:
    selector_id = getattr(selector, "id", None)
    return f"#{selector_id}" if selector_id else f"inline {type(selector).__name__}"


def _feature_label(feature: _Feature) -> str:
    feature_id = getattr(feature, "id", None)
    if feature_id:
        return f"#{feature_id}"
    name = getattr(feature, "name", None)
    if name:
        return f"{type(feature).__name__} {name!r}"
    return f"inline {type(feature).__name__}"


def _url_target(url: str) -> str | None:
    return urlparse.urlparse(url).fragment or None


def _is_local_reference(url: str) -> bool:
    """Return whether ``url`` could refer to a selector in the same document."""
    parsed = urlparse.urlparse(url)
    if parsed.scheme or parsed.netloc:
        return False
    return not parsed.path.lower().endswith((".kml", ".kmz"))


# Handles for the nested complex values that appear inside sub-styles. They
# are flattened to dotted attribute names so each one gets its own provenance.
_NESTED_STYLE_TYPES: Final = {
    "icon": Icon,
    "hot_spot": HotSpot,
}


def _flatten_value(prefix: str, value: object) -> Iterator[tuple[str, Any]]:
    """Yield dotted ``(attribute, value)`` pairs for a sub-style value."""
    if isinstance(value, _XMLObject):
        for attribute, nested in vars(value).items():
            if attribute.startswith("_") or attribute in _IDENTITY_ATTRIBUTES:
                continue
            if nested is None:
                continue
            yield from _flatten_value(f"{prefix}.{attribute}", nested)
        return
    yield prefix, value


def _sub_style_attributes(style: Style) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(sub_style, attribute, value)`` for all non-empty values."""
    for sub_style in style.styles:
        if not isinstance(sub_style, _SUB_STYLE_TYPES):
            continue
        name = type(sub_style).__name__
        for attribute, value in vars(sub_style).items():
            if attribute.startswith("_") or attribute in _IDENTITY_ATTRIBUTES:
                continue
            if value is None:
                continue
            if isinstance(value, _XMLObject):
                yield from (
                    (name, nested_attribute, nested_value)
                    for nested_attribute, nested_value in _flatten_value(
                        attribute,
                        value,
                    )
                )
                continue
            yield name, attribute, value


@dataclass(frozen=True)
class StyleSource:
    """
    Where one effective style attribute came from.

    Attributes
    ----------
    kind : str
        ``"inline"`` for a ``Style`` written inside the feature or ``"shared"``
        for a ``Style`` reached through a ``styleUrl`` or a ``StyleMap``.
    document_id : str | None
        ``id`` of the owning ``Document``, if any.
    feature_id : str | None
        ``id`` of the feature whose style supplied the value.
    style_id : str | None
        ``id`` of the winning ``Style`` declaration.
    style_map_id : str | None
        ``id`` of the nearest ``StyleMap`` traversed to reach the declaration.
    url : str | None
        The ``styleUrl`` value used to reach the declaration.
    mode : PairKey
        State in which the declaration won. Values reused through a
        highlight-to-normal fallback keep ``normal`` here.
    chain : tuple[str, ...]
        Human-readable reference chain, starting at the feature.

    """

    kind: str
    document_id: str | None = None
    feature_id: str | None = None
    style_id: str | None = None
    style_map_id: str | None = None
    url: str | None = None
    mode: PairKey = PairKey.normal
    chain: tuple[str, ...] = ()
    style: _StyleSelector | None = field(default=None, repr=False, compare=False)

    @property
    def origin(self) -> str:
        """Alias for :attr:`kind`."""
        return self.kind

    @property
    def is_inline(self) -> bool:
        """Return whether the value came from an inline ``Style``."""
        return self.kind == "inline"

    @property
    def is_shared(self) -> bool:
        """Return whether the value came from a shared declaration."""
        return self.kind == "shared"

    @property
    def label(self) -> str:
        """Return a compact description of the declaration."""
        if self.kind == "inline":
            owner = f" of feature {self.feature_id!r}" if self.feature_id else ""
            return f"inline Style{owner}"
        target = f"#{self.style_id}" if self.style_id else self.url or "shared Style"
        return f"shared Style {target}"

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable representation without object references."""
        return {
            "kind": self.kind,
            "origin": self.kind,
            "document_id": self.document_id,
            "feature_id": self.feature_id,
            "style_id": self.style_id,
            "style_map_id": self.style_map_id,
            "url": self.url,
            "mode": self.mode.value,
            "chain": list(self.chain),
        }

    def __str__(self) -> str:
        """Return the human-readable label."""
        return self.label


@dataclass(frozen=True)
class StyleDeclaration:
    """
    One winning attribute value and its provenance.

    ``key`` is the stable ``"<SubStyle>.<attribute>"`` identifier, for example
    ``"IconStyle.color"`` or ``"LineStyle.width"``.
    """

    sub_style: str
    attribute: str
    value: Any
    source: StyleSource

    @property
    def key(self) -> str:
        """Return ``"<SubStyle>.<attribute>"``."""
        return f"{self.sub_style}.{self.attribute}"

    @property
    def origin(self) -> str:
        """Shortcut for ``source.kind``."""
        return self.source.kind

    @property
    def source_kind(self) -> str:
        """Shortcut for ``source.kind``."""
        return self.source.kind

    @property
    def style_id(self) -> str | None:
        """Shortcut for ``source.style_id``."""
        return self.source.style_id

    @property
    def style_map_id(self) -> str | None:
        """Shortcut for ``source.style_map_id``."""
        return self.source.style_map_id

    @property
    def url(self) -> str | None:
        """Shortcut for ``source.url``."""
        return self.source.url

    def to_dict(self, *, include_value: bool = True) -> dict[str, Any]:
        """Return a serialisable representation."""
        data: dict[str, Any] = {
            "key": self.key,
            "sub_style": self.sub_style,
            "attribute": self.attribute,
            "source": self.source.to_dict(),
        }
        if include_value:
            data["value"] = self.value
        return data


@dataclass
class ResolvedStyle:
    """
    Effective style for one feature and one state.

    ``declarations`` maps each ``"<SubStyle>.<attribute>"`` key to a
    :class:`StyleDeclaration`. Missing keys mean the feature did not declare
    that attribute; the consuming KML client applies its own default.

    ``fallback_used`` is true when a ``highlight`` resolution reused a
    ``normal`` pair because the map had no usable highlight entry. Reused
    declarations keep ``mode=normal`` so provenance stays honest.
    """

    mode: PairKey
    declarations: dict[str, StyleDeclaration] = field(default_factory=dict)
    fallback_used: bool = False

    @property
    def properties(self) -> dict[str, StyleDeclaration]:
        """Alias for :attr:`declarations`."""
        return self.declarations

    @property
    def provenance(self) -> dict[str, StyleSource]:
        """Return ``{declaration key: source}``."""
        return {
            key: declaration.source for key, declaration in self.declarations.items()
        }

    @property
    def attributes(self) -> dict[str, Any]:
        """Return ``{declaration key: value}``."""
        return {
            key: declaration.value for key, declaration in self.declarations.items()
        }

    @property
    def values(self) -> dict[str, dict[str, Any]]:
        """Return ``{sub_style: {attribute: value}}``."""
        result: dict[str, dict[str, Any]] = {}
        for declaration in self.declarations.values():
            result.setdefault(declaration.sub_style, {})[
                declaration.attribute
            ] = declaration.value
        return result

    @property
    def sources(self) -> dict[str, dict[str, StyleSource]]:
        """Return ``{sub_style: {attribute: source}}``."""
        result: dict[str, dict[str, StyleSource]] = {}
        for declaration in self.declarations.values():
            result.setdefault(declaration.sub_style, {})[
                declaration.attribute
            ] = declaration.source
        return result

    @property
    def sub_styles(self) -> tuple[str, ...]:
        """Return the sub-style names present in this resolution."""
        return tuple(
            dict.fromkeys(
                declaration.sub_style for declaration in self.declarations.values()
            ),
        )

    def declaration(
        self,
        sub_style: str,
        attribute: str | None = None,
    ) -> StyleDeclaration | None:
        """
        Return the declaration for a sub-style attribute.

        ``sub_style`` may be either a bare name plus ``attribute`` or a single
        ``"<SubStyle>.<attribute>"`` key.
        """
        key = sub_style if attribute is None else f"{sub_style}.{attribute}"
        return self.declarations.get(key)

    def get(
        self,
        sub_style: str,
        attribute: str | None = None,
        default: Any = None,
    ) -> Any:
        """Return the effective value for a sub-style attribute."""
        declaration = self.declaration(sub_style, attribute)
        return default if declaration is None else declaration.value

    def source(
        self,
        sub_style: str,
        attribute: str | None = None,
    ) -> StyleSource | None:
        """Return the provenance of a sub-style attribute."""
        declaration = self.declaration(sub_style, attribute)
        return None if declaration is None else declaration.source

    def source_for(self, key: str) -> StyleSource | None:
        """Return the provenance for a ``"SubStyle.attribute"`` key."""
        declaration = self.declarations.get(key)
        return None if declaration is None else declaration.source

    def value(self, key: str, default: Any = None) -> Any:
        """Return the effective value for a ``"SubStyle.attribute"`` key."""
        declaration = self.declarations.get(key)
        return default if declaration is None else declaration.value

    def origin(self, key: str) -> str | None:
        """Return ``source.kind`` for a ``"SubStyle.attribute"`` key."""
        source = self.source_for(key)
        return None if source is None else source.kind

    @property
    def styles(self) -> list[Any]:
        """Return detached sub-style objects for the effective values."""
        return self.to_style().styles

    def sub_style(self, name: str) -> Any | None:
        """Return one detached sub-style by class name, e.g. ``"IconStyle"``."""
        return next(
            (
                sub_style
                for sub_style in self.styles
                if type(sub_style).__name__ == name
            ),
            None,
        )

    def to_style(
        self,
        *,
        id: str | None = None,
        target_id: str | None = None,
    ) -> Style:
        """
        Materialise the effective attributes as a new inline ``Style``.

        The returned object owns copies of all nested values, so editing it
        cannot modify the declarations recorded by the catalog.
        """
        grouped: dict[str, dict[str, Any]] = {}
        for declaration in self.declarations.values():
            grouped.setdefault(declaration.sub_style, {})[
                declaration.attribute
            ] = copy.deepcopy(declaration.value)

        sub_styles: list[Any] = []
        for cls in _SUB_STYLE_TYPES:
            attributes = grouped.pop(cls.__name__, None)
            if not attributes:
                continue
            nested: dict[str, dict[str, Any]] = {}
            direct: dict[str, Any] = {}
            for attribute, value in attributes.items():
                prefix, separator, child = attribute.partition(".")
                if separator and prefix in _NESTED_STYLE_TYPES:
                    nested.setdefault(prefix, {})[child] = value
                else:
                    direct[attribute] = value
            for prefix, children in nested.items():
                direct[prefix] = _NESTED_STYLE_TYPES[prefix](**children)
            sub_styles.append(cls(**direct))
        for name in grouped:
            logger.debug("Dropping unsupported sub-style %s from output", name)

        return Style(id=id, target_id=target_id, styles=sub_styles)

    def __getitem__(self, key: str | tuple[str, str]) -> Any:
        """Return the effective value for a ``"SubStyle.attribute"`` key."""
        if isinstance(key, tuple):
            sub_style, attribute = key
        elif "." in key:
            sub_style, attribute = key.split(".", 1)
        else:
            msg = f"Expected '<SubStyle>.<attribute>', got {key!r}"
            raise KeyError(msg)
        declaration = self.declaration(sub_style, attribute)
        if declaration is None:
            raise KeyError(key)
        return declaration.value

    def __contains__(self, key: object) -> bool:
        """Return whether a declaration key is present."""
        return key in self.declarations

    def __iter__(self) -> Iterator[str]:
        """Iterate over declaration keys."""
        return iter(self.declarations)

    def __len__(self) -> int:
        """Return the number of effective attributes."""
        return len(self.declarations)

    def __bool__(self) -> bool:
        """Return whether any attribute was resolved."""
        return bool(self.declarations)

    def to_dict(self, *, include_value: bool = True) -> dict[str, Any]:
        """Return a serialisable representation."""
        return {
            "mode": self.mode.value,
            "fallback_used": self.fallback_used,
            "declarations": {
                key: declaration.to_dict(include_value=include_value)
                for key, declaration in self.declarations.items()
            },
        }


@dataclass
class FeatureStyleEntry:
    """Normal and highlight style resolutions for one feature."""

    feature: _Feature = field(repr=False, compare=False)
    normal: ResolvedStyle
    highlight: ResolvedStyle

    @property
    def feature_id(self) -> str | None:
        """Return the feature id, if any."""
        return getattr(self.feature, "id", None) or None

    @property
    def feature_name(self) -> str | None:
        """Return the feature name, if any."""
        return getattr(self.feature, "name", None)

    @property
    def styles(self) -> dict[str, ResolvedStyle]:
        """Return both states keyed by ``"normal"``/``"highlight"``."""
        return {
            PairKey.normal.value: self.normal,
            PairKey.highlight.value: self.highlight,
        }

    def for_mode(self, mode: PairKey | str) -> ResolvedStyle:
        """Return the resolution for ``mode``."""
        return self.normal if _coerce_mode(mode) == PairKey.normal else self.highlight

    def resolve(self, mode: PairKey | str = PairKey.normal) -> ResolvedStyle:
        """Alias for :meth:`for_mode`."""
        return self.for_mode(mode)

    def __getitem__(self, mode: PairKey | str) -> ResolvedStyle:
        """Return the resolution for ``mode``."""
        return self.for_mode(mode)

    @property
    def normal_attributes(self) -> dict[str, Any]:
        """Return the normal state's ``{declaration key: value}`` mapping."""
        return self.normal.attributes

    @property
    def highlight_attributes(self) -> dict[str, Any]:
        """Return the highlight state's ``{declaration key: value}`` mapping."""
        return self.highlight.attributes

    @property
    def normal_sources(self) -> dict[str, StyleSource]:
        """Return the normal state's ``{declaration key: source}`` mapping."""
        return self.normal.provenance

    @property
    def highlight_sources(self) -> dict[str, StyleSource]:
        """Return the highlight state's ``{declaration key: source}`` mapping."""
        return self.highlight.provenance

    def sources_for(self, mode: PairKey | str) -> dict[str, StyleSource]:
        """Return the ``{declaration key: source}`` mapping for ``mode``."""
        return self.for_mode(mode).provenance

    def declarations(
        self,
        mode: PairKey | str = PairKey.normal,
    ) -> dict[str, StyleDeclaration]:
        """Return the declarations for ``mode``."""
        return self.for_mode(mode).declarations

    def value(
        self,
        mode: PairKey | str,
        key: str,
        default: Any = None,
    ) -> Any:
        """Return one effective value for ``mode``."""
        return self.for_mode(mode).value(key, default)

    def source(
        self,
        mode: PairKey | str,
        key: str,
    ) -> StyleSource | None:
        """Return the provenance of one value for ``mode``."""
        return self.for_mode(mode).source_for(key)

    def origin(self, mode: PairKey | str, key: str) -> str | None:
        """Return the source kind of one value for ``mode``."""
        return self.for_mode(mode).origin(key)

    @property
    def normal_style(self) -> Style:
        """Return the normal state as a new, detached ``Style``."""
        return self.normal.to_style()

    @property
    def highlight_style(self) -> Style:
        """Return the highlight state as a new, detached ``Style``."""
        return self.highlight.to_style()

    def to_dict(self, *, include_value: bool = True) -> dict[str, Any]:
        """Return a serialisable representation."""
        return {
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "normal": self.normal.to_dict(include_value=include_value),
            "highlight": self.highlight.to_dict(include_value=include_value),
        }


# A descriptive alias: the public object represents all styles of a feature.
FeatureStyles = FeatureStyleEntry


@dataclass(frozen=True)
class StyleReferenceIssue:
    """
    A style reference that could not be followed.

    ``kind`` is ``"cycle"`` for a reference back to a selector already on the
    resolution stack, or ``"missing"`` when no selector exists for the target in
    the feature's document scope.
    """

    kind: str
    url: str
    mode: PairKey
    feature_id: str | None = None
    feature_name: str | None = None
    document_id: str | None = None
    source_style_id: str | None = None
    source_style_map_id: str | None = None
    target: str | None = None
    chain: tuple[str, ...] = ()
    feature: _Feature | None = field(default=None, repr=False, compare=False)

    @property
    def path(self) -> str:
        """Return the reference chain as a readable string."""
        return " -> ".join(self.chain) if self.chain else self.url

    @property
    def message(self) -> str:
        """Return a human-readable explanation."""
        if self.kind == "cycle":
            return f"Style reference cycle in {self.mode.value} state: {self.path}"
        if self.kind == "external":
            return f"External style target {self.url!r} was not fetched: {self.path}"
        return (
            f"Missing style target {self.url!r} in {self.mode.value} state: {self.path}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable representation."""
        return {
            "kind": self.kind,
            "url": self.url,
            "mode": self.mode.value,
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "document_id": self.document_id,
            "source_style_id": self.source_style_id,
            "source_style_map_id": self.source_style_map_id,
            "target": self.target,
            "chain": list(self.chain),
        }

    def __str__(self) -> str:
        """Return the human-readable explanation."""
        return self.message


@dataclass(frozen=True)
class StyleCycle(StyleReferenceIssue):
    """A cycle in the chain of style references."""

    kind: str = field(default="cycle", init=False)


@dataclass(frozen=True)
class MissingStyleTarget(StyleReferenceIssue):
    """A ``styleUrl``/``Pair`` whose target cannot be found in its document."""

    kind: str = field(default="missing", init=False)


@dataclass(frozen=True)
class ExternalStyleReference(StyleReferenceIssue):
    """A reference to a style in another file, which is never fetched."""

    kind: str = field(default="external", init=False)


@dataclass
class _Scope:
    """Document-local namespace for shared style IDs."""

    document: Any = None
    styles: dict[str, _StyleSelector] = field(default_factory=dict)

    @property
    def document_id(self) -> str | None:
        return getattr(self.document, "id", None) or None


@dataclass
class _Layer:
    """A style declaration layer and the point at which it entered the chain."""

    style: Style
    source: StyleSource


def _walk_features(
    root: object,
    root_scope: _Scope,
) -> Iterator[tuple[Any, _Scope, bool]]:
    """
    Yield ``(feature, scope, is_container)`` for every feature in ``root``.

    A new :class:`_Scope` starts at each ``Document``; nested folders inherit
    their document's scope. Leaf features never introduce a scope.
    """
    from fastkml.containers import Document  # noqa: PLC0415
    from fastkml.features import _Feature  # noqa: PLC0415
    from fastkml.kml import KML  # noqa: PLC0415

    stack: list[tuple[Any, _Scope]] = [(root, root_scope)]
    seen: set[int] = set()
    while stack:
        obj, inherited_scope = stack.pop()
        if obj is None:
            continue
        if isinstance(obj, KML):
            stack.extend((child, inherited_scope) for child in reversed(obj.features))
            continue
        if isinstance(obj, Document):
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            scope = _Scope(document=obj)
            yield obj, scope, True
            stack.extend(
                (child, scope)
                for child in reversed(getattr(obj, "features", []) or [])
            )
            continue
        if isinstance(obj, _Feature):
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            children = getattr(obj, "features", None)
            yield obj, inherited_scope, children is not None
            if children:
                stack.extend(
                    (child, inherited_scope) for child in reversed(children)
                )
            continue
        if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            stack.extend((child, inherited_scope) for child in reversed(obj))


def _index_selector(
    selector: object,
    scope: _Scope,
    seen: set[int],
) -> None:
    """Index a selector and nested pair selectors by id, in document order."""
    if not isinstance(selector, (Style, StyleMap)) or id(selector) in seen:
        return
    seen.add(id(selector))
    selector_id = getattr(selector, "id", None)
    if selector_id:
        scope.styles.setdefault(selector_id, selector)
    if isinstance(selector, StyleMap):
        for pair in selector.pairs:
            _index_selector(pair.style, scope, seen)


class _Resolution:
    """Mutable state for resolving one feature in one mode."""

    def __init__(
        self,
        catalog: StyleCatalog,
        feature: _Feature,
        scope: _Scope,
        mode: PairKey,
        reported: set[tuple[str, int, str, tuple[str, ...]]],
    ) -> None:
        self.catalog = catalog
        self.feature = feature
        self.scope = scope
        self.mode = mode
        self.reported = reported
        self.stack: list[tuple[int, int]] = []
        self.chain: list[str] = [_feature_label(feature)]
        self.fallback_used = False

    @property
    def feature_id(self) -> str | None:
        return getattr(self.feature, "id", None) or None

    @property
    def feature_name(self) -> str | None:
        return getattr(self.feature, "name", None)

    def _source(
        self,
        selector: Style,
        *,
        kind: str,
        url: str | None,
        style_map: StyleMap | None,
    ) -> StyleSource:
        return StyleSource(
            kind=kind,
            document_id=self.scope.document_id,
            feature_id=self.feature_id,
            style_id=selector.id or None,
            style_map_id=getattr(style_map, "id", None) or None,
            url=url,
            mode=self.mode,
            chain=tuple(self.chain),
            style=selector,
        )

    def _report(
        self,
        kind: str,
        url: str,
        *,
        target: str | None,
        source_style: _StyleSelector | None = None,
        source_style_map: StyleMap | None = None,
        chain: tuple[str, ...] | None = None,
    ) -> None:
        issue_chain = tuple(self.chain) if chain is None else chain
        # The same cycle/missing reference is reachable while resolving both
        # states (a highlight fallback walks the normal pair). Report the
        # underlying problem once per feature and chain, not once per state.
        key = (kind, id(self.feature), url, issue_chain)
        if key in self.reported:
            return
        self.reported.add(key)
        issue_type = (
            StyleCycle
            if kind == "cycle"
            else ExternalStyleReference
            if kind == "external"
            else MissingStyleTarget
        )
        source_id = getattr(source_style, "id", None) or None
        map_id = getattr(source_style_map, "id", None) or None
        if map_id is None and isinstance(source_style, StyleMap):
            map_id = source_id
        issue = issue_type(
            url=url,
            mode=self.mode,
            feature_id=self.feature_id,
            feature_name=self.feature_name,
            document_id=self.scope.document_id,
            source_style_id=source_id if isinstance(source_style, Style) else None,
            source_style_map_id=map_id,
            target=target,
            chain=issue_chain,
            feature=self.feature,
        )
        if kind == "cycle":
            self.catalog.cycles.append(issue)
        elif kind == "external":
            self.catalog.external.append(issue)
        else:
            self.catalog.missing.append(issue)
        logger.debug("%s", issue.message)

    def _enter(self, selector: _StyleSelector) -> bool:
        key = (id(self.scope), id(selector))
        if key in self.stack:
            return False
        self.stack.append(key)
        return True

    def _leave(self) -> None:
        self.stack.pop()

    def _push(self, label: str) -> None:
        """Append a chain segment unless it repeats the previous one."""
        if not self.chain or self.chain[-1] != label:
            self.chain.append(label)

    def _cycle_chain(self, label: str) -> tuple[str, ...]:
        """Return the current chain with the repeated selector appended."""
        chain = list(self.chain)
        if not chain or chain[-1] != label:
            chain.append(label)
        return tuple(chain)

    def resolve_url(
        self,
        url: str,
        *,
        source_style: _StyleSelector | None = None,
        source_style_map: StyleMap | None = None,
    ) -> list[_Layer]:
        """Resolve one ``styleUrl`` inside the current document scope."""
        target = _url_target(url)
        if not _is_local_reference(url):
            self._report(
                "external",
                url,
                target=target,
                source_style=source_style,
                source_style_map=source_style_map,
                chain=(*self.chain, url or "<empty styleUrl>"),
            )
            return []
        selector = self.scope.styles.get(target) if target else None
        if selector is None:
            self._report(
                "missing",
                url,
                target=target,
                source_style=source_style,
                source_style_map=source_style_map,
                chain=(*self.chain, url or "<empty styleUrl>"),
            )
            return []
        checkpoint = len(self.chain)
        self._push(url)
        try:
            return self.resolve_selector(
                selector,
                origin="shared",
                url=url,
                style_map=source_style_map,
            )
        finally:
            del self.chain[checkpoint:]

    def resolve_selector(
        self,
        selector: _StyleSelector,
        *,
        origin: str,
        url: str | None = None,
        style_map: StyleMap | None = None,
    ) -> list[_Layer]:
        """Resolve a ``Style`` or ``StyleMap`` in the current mode."""
        label = _selector_label(selector)
        # Only StyleMaps can form a reference cycle; a Style has no outgoing
        # reference, so visiting the same Style twice is a duplicate
        # declaration rather than a loop.
        if isinstance(selector, StyleMap) and not self._enter(selector):
            self._report(
                "cycle",
                url or label,
                target=getattr(selector, "id", None) or None,
                source_style=selector,
                source_style_map=style_map,
                chain=self._cycle_chain(label),
            )
            return []
        checkpoint = len(self.chain)
        self._push(label)
        try:
            if isinstance(selector, Style):
                return [
                    _Layer(
                        selector,
                        self._source(
                            selector,
                            kind=origin,
                            url=url,
                            style_map=style_map,
                        ),
                    ),
                ]
            if isinstance(selector, StyleMap):
                return self._resolve_map(selector, origin=origin)
            self._report(
                "missing",
                url or label,
                target=None,
                source_style=selector,
                source_style_map=style_map,
            )
            return []
        finally:
            del self.chain[checkpoint:]
            if isinstance(selector, StyleMap):
                self._leave()

    def _resolve_map(
        self,
        style_map: StyleMap,
        *,
        origin: str,
    ) -> list[_Layer]:
        if self.mode == PairKey.normal:
            return self._resolve_pairs(style_map, PairKey.normal, origin=origin)

        highlight_layers = self._resolve_pairs(
            style_map,
            PairKey.highlight,
            origin=origin,
        )
        if highlight_layers:
            return highlight_layers

        # A highlight state with no usable highlight pair reuses the normal
        # pair. Resolve it with mode temporarily set to ``normal`` so each
        # declaration keeps truthful provenance, then flag the result.
        saved_mode = self.mode
        self.mode = PairKey.normal
        try:
            normal_layers = self._resolve_pairs(
                style_map,
                PairKey.normal,
                origin=origin,
            )
        finally:
            self.mode = saved_mode
        if normal_layers:
            self.fallback_used = True
        return normal_layers

    def _resolve_pairs(
        self,
        style_map: StyleMap,
        key: PairKey,
        *,
        origin: str,
    ) -> list[_Layer]:
        layers: list[_Layer] = []
        pairs = [
            pair
            for pair in style_map.pairs
            if pair.key == key and pair.style is not None
        ]
        for pair in pairs:
            pair_style = pair.style
            self.chain.append(f"{key.value} pair")
            try:
                if isinstance(pair_style, StyleUrl):
                    layers.extend(
                        self.resolve_url(
                            pair_style.url or "",
                            source_style_map=style_map,
                        ),
                    )
                elif isinstance(pair_style, (Style, StyleMap)):
                    layers.extend(
                        self.resolve_selector(
                            pair_style,
                            origin=origin,
                            style_map=style_map,
                        ),
                    )
                else:
                    self._report(
                        "missing",
                        _selector_label(style_map),
                        target=None,
                        source_style_map=style_map,
                    )
            finally:
                self.chain.pop()
        return layers


class StyleCatalog:
    """
    Snapshot of every leaf feature's effective normal and highlight styles.

    The catalog preserves document boundaries: two ``Document`` elements that
    declare styles with the same ``id`` get independent lookup tables, and a
    feature can only resolve references inside its own document. Features that
    are not inside a ``Document`` share one synthetic scope rooted at the object
    passed to the constructor.

    Entries are created for leaf features (``Placemark``, ``NetworkLink``,
    overlays). ``Document`` and ``Folder`` are walked to collect shared styles
    into :attr:`containers`, but they do not get entries of their own.

    Reference cycles, external references, and missing targets are collected
    separately in :attr:`cycles`, :attr:`external`, and :attr:`missing`.
    Resolution continues past a broken reference, so inline overrides and other
    valid declarations still appear in the effective style.

    Parameters
    ----------
    root : object
        A ``KML``, ``Document``, ``Folder``, ``Placemark`` or any other feature.

    Examples
    --------
    >>> catalog = StyleCatalog(document)
    >>> entry = catalog[placemark]
    >>> entry.normal["IconStyle.color"]
    'ff0000ff'
    >>> entry.normal.source("IconStyle", "color").style_id
    'shared-icon'

    """

    def __init__(self, root: object) -> None:
        """Build the catalog by walking ``root`` once."""
        self.root = root
        self.features: list[_Feature] = []
        self.all_features: list[Any] = []
        self.containers: list[Any] = []
        self.documents: list[Any] = []
        self.entries: list[FeatureStyleEntry] = []
        self.cycles: list[StyleCycle] = []
        self.missing: list[MissingStyleTarget] = []
        self.external: list[ExternalStyleReference] = []
        self._scope_by_feature: dict[int, _Scope] = {}
        self._index: dict[int, FeatureStyleEntry] = {}
        self._shared_only: set[int] = set()
        self._issue_keys: set[tuple[str, int, str, tuple[str, ...]]] = set()
        self._build()

    @classmethod
    def from_document(cls, document: object) -> StyleCatalog:
        """Build a catalog rooted at one ``Document``."""
        return cls(document)

    @classmethod
    def from_kml(cls, kml: object) -> StyleCatalog:
        """Build a catalog rooted at a ``KML`` object."""
        return cls(kml)

    @property
    def feature_styles(self) -> list[FeatureStyleEntry]:
        """Alias for :attr:`entries`."""
        return self.entries

    @property
    def feature_entries(self) -> list[FeatureStyleEntry]:
        """Alias for :attr:`entries`."""
        return self.entries

    @property
    def by_feature(self) -> dict[int, FeatureStyleEntry]:
        """Return entries keyed by ``id(feature)``."""
        return dict(self._index)

    @property
    def cycle_issues(self) -> list[StyleCycle]:
        """Alias for :attr:`cycles`."""
        return self.cycles

    @property
    def missing_targets(self) -> list[MissingStyleTarget]:
        """Alias for :attr:`missing`."""
        return self.missing

    @property
    def dangling(self) -> list[StyleReferenceIssue]:
        """Return missing targets and external references."""
        return [*self.external, *self.missing]

    @property
    def issues(self) -> list[StyleReferenceIssue]:
        """Return cycles, external and missing targets in discovery order."""
        return [*self.cycles, *self.external, *self.missing]

    @property
    def unresolved(self) -> list[StyleReferenceIssue]:
        """Alias for :attr:`issues`."""
        return self.issues

    @property
    def cycle_messages(self) -> tuple[str, ...]:
        """Return human-readable cycle descriptions."""
        return tuple(issue.message for issue in self.cycles)

    @property
    def missing_messages(self) -> tuple[str, ...]:
        """Return human-readable missing-target descriptions."""
        return tuple(issue.message for issue in self.missing)

    def _build(self) -> None:
        root_scope = _Scope()
        for obj, scope, is_container in _walk_features(self.root, root_scope):
            self.all_features.append(obj)
            self._scope_by_feature[id(obj)] = scope
            if is_container:
                self.containers.append(obj)
                self._shared_only.add(id(obj))
                if getattr(scope, "document", None) is obj:
                    self.documents.append(obj)
                continue
            self.features.append(obj)

        # Index shared selectors into their owning scope in document order.
        # Document and Folder styles are addressable within their document; a
        # Placemark's styles are inline overrides and must not leak into the
        # lookup table.
        indexed: dict[int, set[int]] = {}
        for container in self.containers:
            scope = self._scope_by_feature[id(container)]
            scope_seen = indexed.setdefault(id(scope), set())
            for selector in getattr(container, "styles", []) or []:
                _index_selector(selector, scope, scope_seen)

        for feature in self.features:
            scope = self._scope_by_feature[id(feature)]
            entry = FeatureStyleEntry(
                feature=feature,
                normal=self._resolve(feature, PairKey.normal, scope),
                highlight=self._resolve(feature, PairKey.highlight, scope),
            )
            self.entries.append(entry)
            self._index[id(feature)] = entry

    def _resolve(
        self,
        feature: _Feature,
        mode: PairKey,
        scope: _Scope,
    ) -> ResolvedStyle:
        state = _Resolution(self, feature, scope, mode, self._issue_keys)
        layers: list[_Layer] = []

        style_url = getattr(feature, "style_url", None)
        if isinstance(style_url, StyleUrl) and style_url.url:
            layers.extend(state.resolve_url(style_url.url, source_style=feature))

        # Inline StyleMap selectors establish the state, then inline Style
        # children apply as per-attribute overrides on top. A Document's styles
        # are shared declarations, not styling for the Document itself.
        if id(feature) not in self._shared_only:
            styles = getattr(feature, "styles", []) or []
            for selector in styles:
                if isinstance(selector, StyleMap):
                    layers.extend(state.resolve_selector(selector, origin="inline"))
            for selector in styles:
                if isinstance(selector, Style):
                    layers.extend(state.resolve_selector(selector, origin="inline"))

        declarations: dict[str, StyleDeclaration] = {}
        for layer in layers:
            for sub_style, attribute, value in _sub_style_attributes(layer.style):
                declaration = StyleDeclaration(
                    sub_style=sub_style,
                    attribute=attribute,
                    value=copy.deepcopy(value),
                    source=layer.source,
                )
                declarations[declaration.key] = declaration
        return ResolvedStyle(
            mode=mode,
            declarations=declarations,
            fallback_used=state.fallback_used,
        )

    def __iter__(self) -> Iterator[FeatureStyleEntry]:
        """Iterate over feature entries in document order."""
        return iter(self.entries)

    def __len__(self) -> int:
        """Return the number of features in the catalog."""
        return len(self.entries)

    def __getitem__(
        self,
        key: int | str | _Feature,
    ) -> FeatureStyleEntry:
        """
        Return an entry by index, feature ``id`` or feature object.

        Feature objects are matched by identity because KML objects are mutable
        and intentionally unhashable.
        """
        if isinstance(key, int):
            return self.entries[key]
        if isinstance(key, str):
            for entry in self.entries:
                if entry.feature_id == key:
                    return entry
            raise KeyError(key)
        entry = self._index.get(id(key))
        if entry is None:
            raise KeyError(key)
        return entry

    def entry(self, feature: _Feature) -> FeatureStyleEntry | None:
        """Return the entry for ``feature`` without raising ``KeyError``."""
        return self._index.get(id(feature))

    def styles_for(self, feature: _Feature) -> dict[str, ResolvedStyle]:
        """Return ``{"normal": ..., "highlight": ...}`` for ``feature``."""
        return self[feature].styles

    def effective_styles(self, feature: _Feature) -> dict[str, ResolvedStyle]:
        """Alias for :meth:`styles_for`."""
        return self.styles_for(feature)

    def provenance(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> dict[str, StyleSource]:
        """Return ``{declaration key: source}`` for ``feature`` and ``mode``."""
        return self.resolve(feature, mode).provenance

    def for_feature(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> ResolvedStyle:
        """Alias for :meth:`resolve`."""
        return self.resolve(feature, mode)

    def resolve(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> ResolvedStyle:
        """Return the resolved style for ``feature`` and ``mode``."""
        entry = self.entry(feature)
        if entry is None:
            msg = "Feature is not part of this catalog"
            raise KeyError(msg)
        return entry.for_mode(mode)

    resolve_style = resolve

    def normal(self, feature: _Feature) -> ResolvedStyle:
        """Return the normal style for ``feature``."""
        return self.resolve(feature, PairKey.normal)

    def highlight(self, feature: _Feature) -> ResolvedStyle:
        """Return the highlight style for ``feature``."""
        return self.resolve(feature, PairKey.highlight)

    def declarations(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> dict[str, StyleDeclaration]:
        """Return the per-attribute declarations for ``feature`` and ``mode``."""
        return self.resolve(feature, mode).declarations

    def value(
        self,
        feature: _Feature,
        sub_style: str,
        attribute: str,
        mode: PairKey | str = PairKey.normal,
        default: Any = None,
    ) -> Any:
        """Return one effective attribute value for ``feature``."""
        return self.resolve(feature, mode).get(sub_style, attribute, default)

    def source(
        self,
        feature: _Feature,
        sub_style: str,
        attribute: str,
        mode: PairKey | str = PairKey.normal,
    ) -> StyleSource | None:
        """Return the provenance of one effective attribute for ``feature``."""
        return self.resolve(feature, mode).source(sub_style, attribute)

    def style_for(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> Style:
        """Return the effective style as a new, detached ``Style`` object."""
        return self.resolve(feature, mode).to_style()

    def normal_style(self, feature: _Feature) -> Style:
        """Return a detached ``Style`` with the effective normal values."""
        return self.style_for(feature, PairKey.normal)

    def highlight_style(self, feature: _Feature) -> Style:
        """Return a detached ``Style`` with the effective highlight values."""
        return self.style_for(feature, PairKey.highlight)

    def effective_style(
        self,
        feature: _Feature,
        mode: PairKey | str = PairKey.normal,
    ) -> Style:
        """Alias for :meth:`style_for`."""
        return self.style_for(feature, mode)

    def to_dict(self, *, include_value: bool = True) -> dict[str, Any]:
        """Return a serialisable representation of the whole catalog."""
        return {
            "features": [
                entry.to_dict(include_value=include_value) for entry in self.entries
            ],
            "cycles": [issue.to_dict() for issue in self.cycles],
            "external": [issue.to_dict() for issue in self.external],
            "missing": [issue.to_dict() for issue in self.missing],
        }

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"features={len(self.features)!r}, "
            f"cycles={len(self.cycles)!r}, "
            f"missing={len(self.missing)!r})"
        )


# Compatibility aliases: "resolver" and "map catalog" describe the same snapshot.
StyleResolver = StyleCatalog
MapStyleCatalog = StyleCatalog


def build_map_catalog(root: object) -> StyleCatalog:
    """Build a :class:`StyleCatalog` from any KML object tree."""
    return StyleCatalog(root)


def build_style_catalog(root: object) -> StyleCatalog:
    """Build a :class:`StyleCatalog` from any KML object tree."""
    return StyleCatalog(root)


def resolve_styles(root: object) -> StyleCatalog:
    """Build a :class:`StyleCatalog` from any KML object tree."""
    return StyleCatalog(root)


def catalog_styles(root: object) -> StyleCatalog:
    """Build a :class:`StyleCatalog` from any KML object tree."""
    return StyleCatalog(root)


def resolve_feature_styles(
    root: object,
    feature: _Feature,
    mode: PairKey | str = PairKey.normal,
) -> ResolvedStyle:
    """
    Resolve one feature's style from a KML tree in a single call.

    Builds a catalog on every call; for repeated lookups use
    :class:`StyleCatalog` directly.
    """
    return StyleCatalog(root).resolve(feature, mode)


def get_style_catalog(root: object) -> StyleCatalog:
    """Build a :class:`StyleCatalog` from any KML object tree."""
    return StyleCatalog(root)


def resolve_style(
    root: object,
    feature: _Feature,
    mode: PairKey | str = PairKey.normal,
) -> ResolvedStyle:
    """
    Resolve one feature's style from a KML tree in a single call.

    This convenience wrapper builds a catalog on every call, which is useful for
    one-off lookups. For repeated lookups build a :class:`StyleCatalog` once.
    """
    return StyleCatalog(root).resolve(feature, mode)
