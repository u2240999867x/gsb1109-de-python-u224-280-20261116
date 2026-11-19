# python-mpd2: MPD library migration planner
#
# Plans the migration of several MPD music libraries into one unified resource
# directory. Local file URIs, stream URLs and relative resources are parsed and
# escaped according to the rules of their own resource type; no single
# normalization function is shared between them, so differences that are
# meaningful to one type (percent escaping, query parameters, literal
# backslashes) can never be silently erased.
#
# Resources are only ever merged when an explicit equivalence evidence record
# links them. Same-named tracks without evidence stay independent. When a
# source mapping admits more than one interpretation, every candidate target is
# retained instead of guessing one.

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlsplit

URL_SCHEMES = ("http", "https", "mms", "mmsh", "mmst", "rtmp", "rtmps", "rtsp")
# file:// URIs and relative names use POSIX style with percent-escaping only.
# A literal backslash inside such a name is therefore an ordinary character,
# never an escape sequence or a path separator. The only recognized escape is
# the percent triplet.
_RESERVED_SEGMENTS = {".", ".."}


class MigrationError(ValueError):
    """Base class for migration planning errors."""


class CollisionError(MigrationError):
    """Two non-equivalent resources resolve to the same target name."""

    def __init__(self, target: str, keys: Sequence[Tuple[str, str]]) -> None:
        sources = ", ".join(
            f"{library}:{original}" for library, original in sorted(keys)
        )
        super().__init__(
            f"target collision at {target!r} between independent resources: {sources}"
        )
        self.target = target
        self.keys = list(keys)


class AmbiguityError(MigrationError):
    """An equivalence group cannot converge because its interpretations differ."""

    def __init__(
        self,
        group: frozenset,
        candidates: Mapping[Tuple[str, str], Sequence[str]],
    ) -> None:
        detail = "; ".join(
            f"{library}:{original} -> {sorted(targets)}"
            for (library, original), targets in sorted(candidates.items())
        )
        super().__init__(f"no common target for evidence-linked resources: {detail}")
        self.group = group
        self.candidates = {key: tuple(targets) for key, targets in candidates.items()}


class StaleMappingError(MigrationError):
    """A root mapping changed after a plan or stored mapping was verified."""


class UnknownRootError(MigrationError):
    """A resource references a library root that has no mapping."""


# ---------------------------------------------------------------------------
# Resource references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRef:
    """One resource exactly as spelled in its source MPD database.

    ``kind`` is one of ``"file"`` (local file URI), ``"url"`` (stream URL) or
    ``"relative"`` (relative resource). ``original`` always retains the source
    spelling and the source domain; it is never normalized away.

    ``segments`` holds the type-specific parsed form: literal POSIX segments
    for file/relative resources (percent escapes decoded, backslashes kept),
    or the single verbatim URL string for streams.
    """

    kind: str
    original: str
    library: str
    source_domain: Optional[str]
    segments: Tuple[str, ...]
    alternatives: Tuple[str, ...] = ()

    @property
    def key(self) -> Tuple[str, str]:
        return (self.library, self.original)


# ---------------------------------------------------------------------------
# Type-specific parsing: one parser per resource type, deliberately separate
# ---------------------------------------------------------------------------


def _split_percent_escaped(value: str) -> Tuple[str, ...]:
    """Split a file-URI path or relative name into literal segments.

    Only percent triplets are unquoted. A literal backslash is an ordinary
    character and is preserved byte for byte; it never separates segments.
    """
    segments: List[str] = []
    for raw in value.split("/"):
        decoded = unquote(raw)
        segments.append(decoded)
    return tuple(segments)


def parse_file_uri(uri: str, library: str) -> ResourceRef:
    """Parse a local ``file://`` URI the way a file resource requires."""
    if not uri.lower().startswith("file://"):
        raise MigrationError(f"not a local file URI: {uri!r}")
    rest = uri[len("file://") :]
    host, separator, path = rest.partition("/")
    if host != "":
        # file://host/path names a remote host; library roots are local.
        raise MigrationError(f"host component is not supported in file URI: {uri!r}")
    if not separator:
        raise MigrationError(f"file URI must use an absolute path: {uri!r}")
    body = _split_percent_escaped(path)
    if any(seg == "" for seg in body):
        raise MigrationError(f"empty path segment in file URI: {uri!r}")
    if any(seg in _RESERVED_SEGMENTS for seg in body):
        raise MigrationError(f"reserved path segment in file URI: {uri!r}")
    return ResourceRef(
        kind="file", original=uri, library=library, source_domain=None, segments=body
    )


def parse_relative(value: str, library: str) -> ResourceRef:
    """Parse a relative resource with the file-style escape rules."""
    if value.startswith("/"):
        raise MigrationError(f"relative resource must not start with '/': {value!r}")
    if "://" in value:
        raise MigrationError(f"relative resource must not carry a scheme: {value!r}")
    segments = _split_percent_escaped(value)
    if any(seg == "" for seg in segments):
        raise MigrationError(f"empty path segment in relative resource: {value!r}")
    if any(seg in _RESERVED_SEGMENTS for seg in segments):
        raise MigrationError(f"reserved path segment in relative resource: {value!r}")
    return ResourceRef(
        kind="relative",
        original=value,
        library=library,
        source_domain=None,
        segments=segments,
    )


def parse_url(url: str, library: str) -> ResourceRef:
    """Parse a stream URL without decoding it or stripping its query."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in URL_SCHEMES or "://" not in url:
        raise MigrationError(f"unsupported stream URL: {url!r}")
    if not parts.netloc:
        raise MigrationError(f"stream URL is missing its host: {url!r}")
    domain = parts.hostname or ""
    # Query and fragment are part of stream identity: URLs differing only
    # there are different streams, and a backslash stays a literal byte.
    return ResourceRef(
        kind="url",
        original=url,
        library=library,
        source_domain=domain,
        segments=(url,),
    )


def parse_resource(value: str, library: str) -> ResourceRef:
    """Dispatch to the parser that matches the resource's own type."""
    if value.lower().startswith("file://"):
        return parse_file_uri(value, library)
    if "://" in value and value.split("://", 1)[0].lower() in URL_SCHEMES:
        return parse_url(value, library)
    return parse_relative(value, library)


def with_alternatives(
    ref: ResourceRef, alternate_spellings: Sequence[str]
) -> ResourceRef:
    """Record additional source spellings for one ambiguous resource.

    Every alternative is parsed with the same type-specific parser, and every
    resulting interpretation is kept as a candidate rather than collapsed.
    """
    parsed = [parse_resource(spelling, ref.library) for spelling in alternate_spellings]
    for other in parsed:
        if other.kind != ref.kind:
            raise MigrationError(
                f"alternative {other.original!r} changes the resource type of "
                f"{ref.original!r}"
            )
    return ResourceRef(
        kind=ref.kind,
        original=ref.original,
        library=ref.library,
        source_domain=ref.source_domain,
        segments=ref.segments,
        alternatives=tuple(alternate_spellings),
    )


# ---------------------------------------------------------------------------
# Type-specific target naming: again, one encoder per resource type
# ---------------------------------------------------------------------------


def _encode_path_segment(segment: str) -> str:
    """Encode one literal segment for the unified POSIX directory.

    File/relative resources only understand percent escaping natively, so the
    encoder uses percent encoding exclusively. A literal backslash becomes
    ``%5C`` and round-trips exactly instead of being read as an escape later.
    """
    if segment in _RESERVED_SEGMENTS:
        raise MigrationError(f"refusing to use reserved path segment: {segment!r}")
    return quote(segment, safe="!$&'()*+,:;=@[]_.-~ ")


def _file_target(ref: ResourceRef, prefix: Tuple[str, ...]) -> Tuple[str, ...]:
    if not ref.segments:
        raise MigrationError(f"file resource without path: {ref.original!r}")
    primary = posixpath.join(
        *(prefix + tuple(_encode_path_segment(seg) for seg in ref.segments))
    )
    return (primary,)


def _relative_target(ref: ResourceRef, prefix: Tuple[str, ...]) -> Tuple[str, ...]:
    primary = posixpath.join(
        *(prefix + tuple(_encode_path_segment(seg) for seg in ref.segments))
    )
    return (primary,)


def _url_target(ref: ResourceRef) -> Tuple[str, ...]:
    """Name a stream slot without decoding the URL or touching its query."""
    domain = ref.source_domain or "no-domain"
    primary = posixpath.join(
        "_streams", _encode_path_segment(domain), _encode_path_segment(ref.original)
    )
    return (primary,)


# ---------------------------------------------------------------------------
# Equivalence evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Equivalence:
    """Evidence that two fully qualified source refs name the same material.

    Both sides are ``(library, original spelling)`` pairs. A checksum and an
    opaque evidence id keep records distinct and auditable.
    """

    left: Tuple[str, str]
    right: Tuple[str, str]
    evidence_id: str
    checksum: Optional[str] = None
    note: str = ""


def _equivalence_groups(
    equivalences: Iterable[Equivalence],
) -> Dict[Tuple[str, str], frozenset]:
    parent: Dict[Tuple[str, str], Tuple[str, str]] = {}

    def find(node: Tuple[str, str]) -> Tuple[str, str]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: Tuple[str, str], b: Tuple[str, str]) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for eq in equivalences:
        union(eq.left, eq.right)

    result: Dict[Tuple[str, str], frozenset] = {}
    for node in list(parent):
        root = find(node)
        result[node] = frozenset(member for member in parent if find(member) == root)
    return result


# ---------------------------------------------------------------------------
# Root mappings with change detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootMapping:
    """Maps one source library root into the unified directory."""

    library: str
    source_root: str
    prefix: Tuple[str, ...]
    digest: str

    @classmethod
    def build(
        cls, library: str, source_root: str, prefix: Sequence[str]
    ) -> "RootMapping":
        clean = tuple(seg for seg in prefix if seg and seg not in _RESERVED_SEGMENTS)
        if not clean:
            raise MigrationError(f"empty target prefix for library {library!r}")
        payload = "\x1f".join([library, source_root, "/".join(clean)])
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(library, source_root, clean, digest)


@dataclass
class RootCatalog:
    """All root mappings, versioned so stale plans can be detected."""

    mappings: Dict[str, RootMapping]
    version: str

    @classmethod
    def from_mappings(
        cls, mappings: Mapping[str, RootMapping], version: str
    ) -> "RootCatalog":
        return cls(mappings=dict(mappings), version=version)


# ---------------------------------------------------------------------------
# Plan data structures
# ---------------------------------------------------------------------------


@dataclass
class TargetEntry:
    group: frozenset
    refs: List[ResourceRef] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    checksums: List[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    catalog_version: str
    root_digests: Dict[str, str]
    collisions: List[CollisionError]
    ambiguities: List[AmbiguityError]
    entries: List[TargetEntry]
    forward: Dict[Tuple[str, str], Tuple[str, ...]]
    reverse: Dict[str, Tuple[ResourceRef, ...]]

    def targets_for(self, library: str, original: str) -> Tuple[str, ...]:
        return self.forward.get((library, original), ())

    def is_stale_against(self, catalog: RootCatalog) -> bool:
        """True when any root mapping changed after this plan was verified."""
        if catalog.version != self.catalog_version:
            return True
        for library, digest in self.root_digests.items():
            current = catalog.mappings.get(library)
            if current is None or current.digest != digest:
                return True
        return False


class MigrationPlanner:
    def __init__(
        self,
        catalog: RootCatalog,
        equivalences: Iterable[Equivalence] = (),
    ) -> None:
        self.catalog = catalog
        self.equivalences = list(equivalences)
        self.groups = _equivalence_groups(self.equivalences)
        self._evidence_pairs = {
            frozenset((eq.left, eq.right)): eq for eq in self.equivalences
        }

    def _group_of(self, key: Tuple[str, str]) -> frozenset:
        return self.groups.get(key, frozenset({key}))

    def _evidence_for(self, group: frozenset) -> Tuple[List[str], List[str]]:
        ids: List[str] = []
        checksums: List[str] = []
        for pair, eq in self._evidence_pairs.items():
            if pair <= group:
                ids.append(eq.evidence_id)
                if eq.checksum is not None:
                    checksums.append(eq.checksum)
        return sorted(ids), sorted(set(checksums))

    def _candidate_targets(self, ref: ResourceRef) -> Tuple[str, ...]:
        try:
            mapping = self.catalog.mappings[ref.library]
        except KeyError as exc:
            raise UnknownRootError(
                f"no root mapping for library {ref.library!r}"
            ) from exc
        if ref.kind == "file":
            primary = _file_target(ref, mapping.prefix)
        elif ref.kind == "relative":
            primary = _relative_target(ref, mapping.prefix)
        elif ref.kind == "url":
            # Streams carry their source domain in the slot and bypass the
            # per-library file prefix.
            primary = _url_target(ref)
        else:  # pragma: no cover - guarded by the parsers
            raise MigrationError(f"unknown resource kind: {ref.kind!r}")

        candidates = list(primary)
        for spelling in ref.alternatives:
            alternative = parse_resource(spelling, ref.library)
            if alternative.kind == "url":
                candidates.extend(_url_target(alternative))
            elif alternative.kind == "file":
                candidates.extend(_file_target(alternative, mapping.prefix))
            else:
                candidates.extend(_relative_target(alternative, mapping.prefix))
        # Preserve order while removing exact duplicates.
        return tuple(dict.fromkeys(candidates))

    def plan(self, resources: Iterable[ResourceRef]) -> MigrationPlan:
        refs = list(resources)
        grouped: Dict[frozenset, List[ResourceRef]] = {}
        for ref in refs:
            grouped.setdefault(self._group_of(ref.key), []).append(ref)

        collisions: List[CollisionError] = []
        ambiguities: List[AmbiguityError] = []
        entries: List[TargetEntry] = []
        forward: Dict[Tuple[str, str], Tuple[str, ...]] = {}

        for group, members in grouped.items():
            per_ref: Dict[Tuple[str, str], Tuple[str, ...]] = {}
            for ref in members:
                targets = self._candidate_targets(ref)
                per_ref[ref.key] = targets
                forward[ref.key] = targets

            evidence_ids, checksums = self._evidence_for(group)
            entry = TargetEntry(
                group=group,
                refs=list(members),
                evidence_ids=evidence_ids,
                checksums=checksums,
            )

            if len(group) > 1:
                common = set(per_ref[members[0].key])
                for ref in members[1:]:
                    common &= set(per_ref[ref.key])
                if not common:
                    # Multiple interpretations with no shared target: keep all
                    # candidates and flag it instead of merging on a guess.
                    ambiguities.append(AmbiguityError(group, per_ref))
                    entry.targets = sorted(
                        {target for targets in per_ref.values() for target in targets}
                    )
                else:
                    entry.targets = sorted(common)
            else:
                entry.targets = list(per_ref[members[0].key])
            entries.append(entry)

        # Whole-batch collision detection first: a target claimed by two
        # different equivalence groups. Same-named, evidence-less tracks land
        # here and are never silently merged.
        owners: Dict[str, List[frozenset]] = {}
        for entry in entries:
            for target in entry.targets:
                owners.setdefault(target, []).append(entry.group)
        for target, owner_groups in sorted(owners.items()):
            distinct = set(owner_groups)
            if len(distinct) > 1:
                keys = sorted(key for group in distinct for key in group)
                collisions.append(CollisionError(target, keys))

        # Reverse mapping: every target lists the distinct originals behind it.
        reverse: Dict[str, List[ResourceRef]] = {}
        for entry in entries:
            for target in entry.targets:
                bucket = reverse.setdefault(target, [])
                for ref in entry.refs:
                    if ref not in bucket:
                        bucket.append(ref)

        return MigrationPlan(
            catalog_version=self.catalog.version,
            root_digests={
                library: mapping.digest
                for library, mapping in self.catalog.mappings.items()
            },
            collisions=collisions,
            ambiguities=ambiguities,
            entries=sorted(entries, key=lambda item: sorted(item.targets)),
            forward=forward,
            reverse={
                target: tuple(sorted(bucket, key=lambda ref: ref.key))
                for target, bucket in sorted(reverse.items())
            },
        )


# ---------------------------------------------------------------------------
# Applied mapping: directory lookups and re-verification after root changes
# ---------------------------------------------------------------------------


@dataclass
class StoredMapping:
    """The applied mapping, pinned to the root digests at apply time."""

    catalog_version: str
    root_digests: Dict[str, str]
    forward: Dict[Tuple[str, str], Tuple[str, ...]]
    reverse: Dict[str, Tuple[Tuple[str, str], ...]]

    @classmethod
    def from_plan(cls, plan: MigrationPlan) -> "StoredMapping":
        return cls(
            catalog_version=plan.catalog_version,
            root_digests=dict(plan.root_digests),
            forward={key: targets for key, targets in plan.forward.items()},
            reverse={
                target: tuple(ref.key for ref in refs)
                for target, refs in plan.reverse.items()
            },
        )

    def lookup_original(self, target: str) -> Tuple[Tuple[str, str], ...]:
        """Directory query: target path back to the original material(s)."""
        return self.reverse.get(target, ())

    def lookup_targets(self, library: str, original: str) -> Tuple[str, ...]:
        return self.forward.get((library, original), ())

    def verify(self, catalog: RootCatalog) -> None:
        """Refuse to trust this mapping once a root mapping has changed."""
        if catalog.version != self.catalog_version:
            raise StaleMappingError(
                f"catalog version changed: {self.catalog_version!r} -> "
                f"{catalog.version!r}; re-verify the migration plan"
            )
        for library, digest in self.root_digests.items():
            current = catalog.mappings.get(library)
            if current is None:
                raise StaleMappingError(
                    f"library {library!r} vanished from the root catalog; "
                    "re-verify the migration plan"
                )
            if current.digest != digest:
                raise StaleMappingError(
                    f"root mapping for {library!r} changed; re-verify the "
                    "migration plan before trusting the stored mapping"
                )

    def originals_remain_distinct(
        self, keys: Iterable[Tuple[str, str]]
    ) -> bool:
        """True while the reverse mapping still separates these resources."""
        claimed: set = set()
        for key in keys:
            for target in self.forward.get(key, ()):
                if target in claimed:
                    return False
                claimed.add(target)
        return True
