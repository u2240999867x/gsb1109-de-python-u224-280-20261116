"""Capacity census for (partially scanned) NTFS volumes.

A capacity census reports three things at once:

- the number of file entities (in-use MFT records);
- the number of directory links and visible (fully proven) paths;
- the number of directory links that may still be hiding in unscanned territory.

The census is built from three evidence sources:

- the MFT: file entities, their declared link counts (the record header
  ``ReferenceCount``) and their ``$FILE_NAME`` attributes;
- directory indexes: ``$I30`` entries of the parent directories that were scanned;
- the set of parent directories that have been fully scanned (scan coverage).

Attribution rules:

- Everything is attributed by *full* file reference (segment number and sequence
  number). A matching segment number with a different sequence number is a stale
  observation: sequence number reuse can never prove a relation for a previous
  life of a segment.
- A DOS (8.3) short name and a long (Win32/POSIX) name observed for the same
  child in the same parent directory fold into a single directory link. The fold
  only happens with that evidence in hand; a lone short or long name is a link
  of its own.
- When the declared link count of an entity disagrees with the observed names,
  the gap is reported together with provable bounds based on scan coverage.
  Missing links can only hide in parent directories that were not scanned; no
  volume-wide extrapolation is made from any single parent directory.
- A missing or cyclic parent chain only invalidates the path proof of the
  affected link. The link itself, and all other links, still count.
- Every reported count traces back to actual observed names or to explicitly
  uncovered parent directories. Declared counts are never used to fabricate
  paths that were not observed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, IntEnum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dissect.ntfs.mft import Mft
    from dissect.ntfs.ntfs import NTFS

# Kept literal (equal to FILE_NUMBER_ROOT) so this module stays importable on its own
DEFAULT_ROOT_SEGMENT = 5


class Namespace(IntEnum):
    """``$FILE_NAME`` namespace rules."""

    POSIX = 0x00
    WIN32 = 0x01
    DOS = 0x02
    WIN32_AND_DOS = 0x03


class EvidenceSource(Enum):
    """Where a name was observed."""

    MFT = auto()
    INDEX = auto()


class PathStatus(Enum):
    """The outcome of proving the full path of a directory link."""

    PROVEN = auto()
    MISSING_PARENT = auto()
    CYCLE = auto()
    UNNAMED_PARENT = auto()


@dataclass(frozen=True, order=True)
class FileReference:
    """A full MFT file reference: segment number and sequence number.

    Attribution is always by full reference. A matching segment number with a
    different sequence number refers to a previous life of that segment and is
    never used to prove a relation.
    """

    segment: int
    sequence: int

    def __repr__(self) -> str:
        return f"<FileReference {self.segment}#{self.sequence}>"


@dataclass(frozen=True)
class NameEvidence:
    """An observed name, merged across all evidence sources that reported it."""

    child: FileReference
    parent: FileReference
    name: str
    namespace: int
    sources: frozenset[EvidenceSource]


@dataclass(frozen=True)
class DirectoryLink:
    """A single directory link, backed by one or two observed names.

    A long (Win32/POSIX) name and a DOS (8.3) short name observed for the same
    child in the same parent directory fold into a single link. The ``names``
    tuple keeps the actual names backing the link, display name first.
    """

    child: FileReference
    parent: FileReference
    names: tuple[str, ...]
    sources: frozenset[EvidenceSource]
    path: str | None
    path_status: PathStatus

    @property
    def name(self) -> str:
        """Return the display name of this link (the long name if a short name was folded in)."""
        return self.names[0]


@dataclass(frozen=True)
class EntityLinks:
    """Per-entity link accounting.

    ``lower_bound`` is the number of links proven by actual observed names.
    ``upper_bound`` additionally allows for links that may still hide in
    unscanned parent directories. ``phantom_links`` is the part of the declared
    link count that is provably without names because scan coverage is complete.
    """

    reference: FileReference
    is_directory: bool
    declared_links: int | None
    links: tuple[DirectoryLink, ...]
    observed_links: int
    gap: int | None
    possibly_missed: int
    phantom_links: int
    lower_bound: int
    upper_bound: int


@dataclass(frozen=True)
class CapacityReport:
    """Volume-level capacity report.

    Every count traces back to actual observed names (``entities``, their
    ``links`` and ``visible_paths``) or to explicitly uncovered ranges
    (``unscanned_directories`` behind ``missed_links_upper``).
    """

    entity_count: int
    link_count: int
    visible_path_count: int
    visible_paths: tuple[str, ...]
    missed_links_lower: int
    missed_links_upper: int
    entities: tuple[EntityLinks, ...]
    stale_names: tuple[NameEvidence, ...]
    unattributed_names: tuple[NameEvidence, ...]
    scanned_parents: frozenset[FileReference]
    unscanned_directories: tuple[FileReference, ...]

    def entity(self, reference: FileReference) -> EntityLinks | None:
        """Return the :class:`EntityLinks` for the given full file reference, if any."""
        return next((entity for entity in self.entities if entity.reference == reference), None)


@dataclass(frozen=True)
class _Entity:
    reference: FileReference
    is_directory: bool
    declared_links: int | None
    in_use: bool


@dataclass(frozen=True)
class _RawLink:
    child: FileReference
    parent: FileReference
    names: tuple[str, ...]
    sources: frozenset[EvidenceSource]


def _evidence_key(evidence: NameEvidence) -> tuple:
    return (evidence.child, evidence.parent, evidence.name, evidence.namespace)


def _fold_names(child: FileReference, names: list[NameEvidence]) -> list[_RawLink]:
    """Fold observed names of one entity into directory links.

    A DOS short name folds with a long name only when both are observed for the
    same child in the same parent directory. Pairing is one-to-one and
    deterministic; unpaired names remain links of their own.
    """
    by_parent: dict[FileReference, list[NameEvidence]] = defaultdict(list)
    for evidence in names:
        by_parent[evidence.parent].append(evidence)

    links = []
    for parent in sorted(by_parent):
        group = sorted(by_parent[parent], key=lambda evidence: (evidence.name, evidence.namespace))
        dos = [evidence for evidence in group if evidence.namespace == Namespace.DOS]
        combined = [evidence for evidence in group if evidence.namespace == Namespace.WIN32_AND_DOS]
        long = [evidence for evidence in group if evidence not in dos and evidence not in combined]

        pairs = min(len(dos), len(long))
        links.extend(
            _RawLink(child, parent, (long[i].name, dos[i].name), long[i].sources | dos[i].sources) for i in range(pairs)
        )
        links.extend(_RawLink(child, parent, (evidence.name,), evidence.sources) for evidence in long[pairs:])
        links.extend(_RawLink(child, parent, (evidence.name,), evidence.sources) for evidence in dos[pairs:])
        links.extend(_RawLink(child, parent, (evidence.name,), evidence.sources) for evidence in combined)

    return sorted(links, key=lambda link: (link.parent, link.names))


class CapacityCensus:
    """Collect capacity evidence and build a :class:`CapacityReport`.

    Args:
        scanned_parents: Full references of parent directories whose contents
            have been fully enumerated (the scan coverage).
        root_segment: The segment number of the root directory.
    """

    def __init__(self, scanned_parents: Iterable[FileReference] = (), root_segment: int = DEFAULT_ROOT_SEGMENT):
        self.root_segment = root_segment
        self._entities: dict[FileReference, _Entity] = {}
        self._names: list[NameEvidence] = []
        self._scanned_parents: set[FileReference] = set(scanned_parents)

    def add_entity(
        self,
        reference: FileReference,
        *,
        is_directory: bool = False,
        declared_links: int | None = None,
        in_use: bool = True,
    ) -> None:
        """Register a file entity (an MFT record) with its declared link count."""
        self._entities[reference] = _Entity(reference, is_directory, declared_links, in_use)

    def add_name(
        self,
        *,
        child: FileReference,
        parent: FileReference,
        name: str,
        namespace: int,
        source: EvidenceSource,
    ) -> None:
        """Register an observed name from the MFT or from a directory index."""
        self._names.append(NameEvidence(child, parent, name, namespace, frozenset({source})))

    def mark_parent_scanned(self, reference: FileReference) -> None:
        """Mark a parent directory as fully enumerated."""
        self._scanned_parents.add(reference)

    def report(self) -> CapacityReport:
        """Build the capacity report from all collected evidence."""
        entities = {ref: entity for ref, entity in self._entities.items() if entity.in_use}
        entity_segments = {ref.segment for ref in entities}

        # Merge duplicate observations of the same name into a single evidence set
        merged: dict[tuple, NameEvidence] = {}
        for observation in self._names:
            key = _evidence_key(observation)
            if key in merged:
                observation = NameEvidence(*key, merged[key].sources | observation.sources)
            merged[key] = observation

        live: dict[FileReference, list[NameEvidence]] = defaultdict(list)
        stale: list[NameEvidence] = []
        unattributed: list[NameEvidence] = []
        for evidence in merged.values():
            if evidence.child in entities:
                live[evidence.child].append(evidence)
            elif evidence.child.segment in entity_segments:
                # The segment is in use by a newer incarnation: a stale observation
                stale.append(evidence)
            else:
                unattributed.append(evidence)

        # Fold observed names into directory links per entity
        raw_links = {ref: _fold_names(ref, names) for ref, names in live.items()}

        # Prove the path of each link independently; a broken chain only
        # affects its own link, all other links still count
        links: dict[FileReference, tuple[DirectoryLink, ...]] = {}
        for ref, raws in raw_links.items():
            links[ref] = tuple(self._build_link(raw, raw_links, entities) for raw in raws)

        unscanned_directories = tuple(
            sorted(ref for ref, entity in entities.items() if entity.is_directory and ref not in self._scanned_parents)
        )
        coverage_complete = not unscanned_directories

        entity_reports = []
        for ref in sorted(entities):
            entity = entities[ref]
            entity_links = links.get(ref, ())
            observed = len(entity_links)
            declared = entity.declared_links

            if declared is None:
                gap = None
                possibly_missed = 0
                phantom = 0
            else:
                gap = declared - observed
                # Missing links can only hide in unscanned parent directories;
                # with complete coverage the gap is provably phantom
                possibly_missed = max(0, gap) if not coverage_complete else 0
                phantom = max(0, gap) if coverage_complete else 0

            entity_reports.append(
                EntityLinks(
                    reference=ref,
                    is_directory=entity.is_directory,
                    declared_links=declared,
                    links=entity_links,
                    observed_links=observed,
                    gap=gap,
                    possibly_missed=possibly_missed,
                    phantom_links=phantom,
                    lower_bound=observed,
                    upper_bound=observed + possibly_missed,
                )
            )

        visible_paths = tuple(
            sorted({link.path for entity_links in links.values() for link in entity_links if link.path is not None})
        )

        return CapacityReport(
            entity_count=len(entities),
            link_count=sum(entity.observed_links for entity in entity_reports),
            visible_path_count=len(visible_paths),
            visible_paths=visible_paths,
            missed_links_lower=0,
            missed_links_upper=sum(entity.possibly_missed for entity in entity_reports),
            entities=tuple(entity_reports),
            stale_names=tuple(sorted(stale, key=_evidence_key)),
            unattributed_names=tuple(sorted(unattributed, key=_evidence_key)),
            scanned_parents=frozenset(self._scanned_parents),
            unscanned_directories=unscanned_directories,
        )

    def _build_link(
        self,
        raw: _RawLink,
        raw_links: dict[FileReference, list[_RawLink]],
        entities: dict[FileReference, _Entity],
    ) -> DirectoryLink:
        path, status = self._prove_path(raw, raw_links, entities)
        return DirectoryLink(raw.child, raw.parent, raw.names, raw.sources, path, status)

    def _prove_path(
        self,
        link: _RawLink,
        raw_links: dict[FileReference, list[_RawLink]],
        entities: dict[FileReference, _Entity],
    ) -> tuple[str | None, PathStatus]:
        """Walk the parent chain of a link to prove its full path.

        Every step requires a full reference match; a segment whose sequence
        number was reused does not prove the old relation. Only actual observed
        names are used to build the path.
        """
        parts = [link.names[0]]
        seen = {link.child}
        ref = link.parent

        while True:
            if ref.segment == self.root_segment and ref in entities:
                return "\\".join(reversed(parts)), PathStatus.PROVEN

            if ref in seen:
                return None, PathStatus.CYCLE
            seen.add(ref)

            if ref not in entities:
                return None, PathStatus.MISSING_PARENT

            parent_links = raw_links.get(ref)
            if not parent_links:
                return None, PathStatus.UNNAMED_PARENT

            next_link = parent_links[0]
            parts.append(next_link.names[0])
            ref = next_link.parent

    @classmethod
    def from_ntfs(cls, ntfs: NTFS, index_parents: Iterable[int | FileReference] | None = None) -> CapacityCensus:
        """Build a census from an :class:`~dissect.ntfs.ntfs.NTFS` filesystem.

        Args:
            ntfs: The filesystem to enumerate.
            index_parents: Segments or full references of parent directories whose
                ``$I30`` index is fully enumerated. ``None`` (default) scans every
                in-use directory; an empty iterable scans nothing (MFT-only census).
        """
        return cls.from_mft(ntfs.mft, index_parents)

    @classmethod
    def from_mft(cls, mft: Mft, index_parents: Iterable[int | FileReference] | None = None) -> CapacityCensus:
        """Build a census from an :class:`~dissect.ntfs.mft.Mft`.

        Args:
            mft: The MFT to enumerate.
            index_parents: Segments or full references of parent directories whose
                ``$I30`` index is fully enumerated. ``None`` (default) scans every
                in-use directory; an empty iterable scans nothing (MFT-only census).
        """
        from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, FILE_RECORD_SEGMENT_IN_USE
        from dissect.ntfs.exceptions import Error
        from dissect.ntfs.util import segment_reference

        census = cls()
        records = {}

        for record in mft.segments():
            if not record.header.Flags & FILE_RECORD_SEGMENT_IN_USE:
                continue

            try:
                attributes = record.attributes
            except Error:
                continue

            ref = FileReference(record.segment, record.header.SequenceNumber)
            census.add_entity(ref, is_directory=record.is_dir(), declared_links=record.header.ReferenceCount)

            for attr in attributes[ATTRIBUTE_TYPE_CODE.FILE_NAME]:
                parent = attr.attr.ParentDirectory
                census.add_name(
                    child=ref,
                    parent=FileReference(segment_reference(parent), parent.SequenceNumber),
                    name=attr.file_name,
                    namespace=attr.flags,
                    source=EvidenceSource.MFT,
                )

            records[record.segment] = record

        if index_parents is None:
            scan_segments = [segment for segment, record in records.items() if record.is_dir()]
        else:
            scan_segments = [ref.segment if isinstance(ref, FileReference) else ref for ref in index_parents]

        for segment in scan_segments:
            record = records.get(segment)
            if record is None or not record.is_dir():
                continue

            ref = FileReference(record.segment, record.header.SequenceNumber)
            try:
                observations = []
                for entry in record.index("$I30").entries():
                    attribute = entry.attribute
                    if attribute is None:
                        continue

                    child = entry.header.FileReference
                    observations.append(
                        {
                            "child": FileReference(segment_reference(child), child.SequenceNumber),
                            "parent": ref,
                            "name": attribute.file_name,
                            "namespace": attribute.flags,
                            "source": EvidenceSource.INDEX,
                        }
                    )
            except (EOFError, Error):
                # The index is (partially) unreadable, so this parent stays uncovered
                continue

            for kwargs in observations:
                census.add_name(**kwargs)

            census.mark_parent_scanned(ref)

        return census
