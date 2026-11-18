"""Offline capacity reporting for NTFS volumes.

Builds a cluster-level capacity report by combining non-resident attribute runlists
(``$DATA`` and other stream-like attributes), compression units, valid data lengths and
the volume allocation bitmap (``$Bitmap``).

The report distinguishes:

- logical stream length (per stream, named streams counted independently);
- stream declared allocation (``AllocatedLength``) and runlist-claimed clusters;
- the actual physical union of all claimed clusters (a cluster claimed by multiple
  streams is only counted once on disk, but all claimants are preserved as conflicts);
- ranges that are inconsistent with the volume allocation bitmap;
- bitmap-allocated clusters with no known owner (e.g. caused by missing extension
  records) versus definitively free clusters.

Note that with partial data (unreadable or incomplete MFT records, no ``$Bitmap``) an
exact volume-wide free space number cannot be derived; :attr:`CapacityReport.complete`
indicates whether the reported free space is exact.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dissect.ntfs.c_ntfs import (
    ATTRIBUTE_FLAG_COMPRESSION_MASK,
    ATTRIBUTE_FLAG_SPARSE,
    ATTRIBUTE_TYPE_CODE,
    DEFAULT_CLUSTER_SIZE,
    FILE_NUMBER_BITMAP,
    FILE_NUMBER_MFT,
    FILE_RECORD_SEGMENT_IN_USE,
)
from dissect.ntfs.exceptions import MftNotAvailableError
from dissect.ntfs.util import segment_reference

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from dissect.ntfs.attr import Attribute
    from dissect.ntfs.mft import MftRecord
    from dissect.ntfs.ntfs import NTFS


class IntervalSet:
    """A set of half-open ``[start, end)`` cluster ranges, kept sorted and disjoint."""

    __slots__ = ("_ranges",)

    def __init__(self, ranges: Iterable[tuple[int, int]] = ()):
        self._ranges: list[tuple[int, int]] = []
        for start, end in ranges:
            self.add(start, end)

    def __repr__(self) -> str:
        return f"<IntervalSet ranges={len(self._ranges)} total={self.total()}>"

    def __iter__(self) -> Iterator[tuple[int, int]]:
        return iter(self._ranges)

    def __len__(self) -> int:
        return len(self._ranges)

    def __bool__(self) -> bool:
        return bool(self._ranges)

    def __contains__(self, cluster: int) -> bool:
        idx = bisect_right(self._ranges, (cluster, float("inf"))) - 1
        return idx >= 0 and self._ranges[idx][1] > cluster

    def add(self, start: int, end: int) -> None:
        """Add the half-open range ``[start, end)``, merging overlapping or adjacent ranges."""
        if end <= start:
            return

        ranges = self._ranges
        idx = bisect_left(ranges, (start, end))
        if idx > 0 and ranges[idx - 1][1] >= start:
            idx -= 1
            start = ranges[idx][0]
            end = max(end, ranges[idx][1])

        stop = idx
        while stop < len(ranges) and ranges[stop][0] <= end:
            end = max(end, ranges[stop][1])
            stop += 1

        ranges[idx:stop] = [(start, end)]

    def total(self) -> int:
        """Return the total number of clusters covered by this set."""
        return sum(end - start for start, end in self._ranges)

    def intersection(self, other: IntervalSet) -> IntervalSet:
        """Return a new :class:`IntervalSet` with the ranges present in both sets."""
        result = IntervalSet()
        for start, end in self._ranges:
            for other_start, other_end in other._ranges:
                if other_end <= start:
                    continue
                if other_start >= end:
                    break
                result.add(max(start, other_start), min(end, other_end))
        return result

    def difference(self, other: IntervalSet) -> IntervalSet:
        """Return a new :class:`IntervalSet` with the ranges of this set minus the ranges of ``other``."""
        result = IntervalSet()
        for start, end in self._ranges:
            cursor = start
            for other_start, other_end in other._ranges:
                if other_end <= cursor:
                    continue
                if other_start >= end:
                    break
                if other_start > cursor:
                    result.add(cursor, other_start)
                cursor = max(cursor, other_end)
                if cursor >= end:
                    break
            if cursor < end:
                result.add(cursor, end)
        return result


@dataclass(frozen=True)
class ClusterClaim:
    """A physical cluster range claimed by a single stream runlist entry.

    Traces back to the original attribute through ``segment`` (MFT record number) and
    ``instance`` (attribute instance within that record).
    """

    segment: int
    attribute_type: ATTRIBUTE_TYPE_CODE
    name: str
    instance: int
    vcn: int
    lcn: int
    length: int
    compressed: bool

    @property
    def end(self) -> int:
        """Return the first cluster after this claim."""
        return self.lcn + self.length


@dataclass
class StreamAllocation:
    """Allocation information for a single stream (attribute type + name) of an MFT record.

    ``physical_clusters`` counts this stream's own runlist clusters; clusters that are
    (erroneously) claimed by multiple streams are counted per stream here and only
    de-duplicated in the report-level physical union.
    """

    segment: int
    attribute_type: ATTRIBUTE_TYPE_CODE
    name: str
    resident: bool = False
    compressed: bool = False
    sparse: bool = False
    compression_unit: int = 0
    logical_size: int = 0
    valid_data_length: int = 0
    allocated_size: int = 0
    sparse_clusters: int = 0
    claims: list[ClusterClaim] = field(default_factory=list)
    sized: bool = False

    @property
    def physical_clusters(self) -> int:
        """Return the number of physical clusters claimed by this stream's runlists."""
        return sum(claim.length for claim in self.claims)


@dataclass(frozen=True)
class ConflictRange:
    """A physical cluster range claimed by more than one stream."""

    lcn: int
    length: int
    claims: tuple[ClusterClaim, ...]

    @property
    def end(self) -> int:
        """Return the first cluster after this conflicted range."""
        return self.lcn + self.length


@dataclass(frozen=True)
class BitmapPosition:
    """The allocation state and exact position of a cluster in the volume ``$Bitmap``."""

    lcn: int
    allocated: bool
    byte_offset: int
    bit_offset: int


@dataclass
class RangeLookup:
    """Result of looking up a physical cluster range in a :class:`CapacityReport`."""

    lcn: int
    length: int
    claims: list[ClusterClaim]
    conflicts: list[ConflictRange]
    bitmap: list[BitmapPosition] | None

    @property
    def conflict(self) -> bool:
        """Return whether any cluster in the looked up range is claimed by multiple streams."""
        return bool(self.conflicts)


class CapacityReport:
    """Offline capacity report of an NTFS volume.

    Args:
        cluster_size: The cluster size of the volume in bytes.
        total_clusters: The total number of clusters of the volume, if known.
    """

    def __init__(self, cluster_size: int = DEFAULT_CLUSTER_SIZE, total_clusters: int | None = None):
        self.cluster_size = cluster_size
        self.total_clusters = total_clusters

        self.streams: list[StreamAllocation] = []
        self.claims: list[ClusterClaim] = []
        self.bitmap: bytes | None = None

        self.deleted_segments = 0
        self.extension_segments = 0
        self.unreadable_segments: list[int] = []
        self.incomplete_segments: list[int] = []

        self._stream_map: dict[tuple[int, int, str], StreamAllocation] = {}

        # Derived values, computed by finalize()
        self.physical_union = IntervalSet()
        self.conflicts: list[ConflictRange] = []
        self.bitmap_allocated: IntervalSet | None = None
        self.claimed_not_allocated = IntervalSet()
        self.allocated_not_claimed = IntervalSet()
        self.free: IntervalSet | None = None

    @classmethod
    def from_ntfs(cls, ntfs: NTFS) -> CapacityReport:
        """Build a capacity report from an :class:`~dissect.ntfs.ntfs.NTFS` instance.

        Walks all MFT records and, if available, the volume ``$Bitmap``. Records that
        cannot be parsed (e.g. because of missing extension records) are tracked in
        :attr:`unreadable_segments` and :attr:`incomplete_segments`; their clusters may
        show up as allocated-but-unclaimed ("unknown ownership") and exact free space
        can no longer be derived.
        """
        total_clusters = None
        if ntfs.boot_sector:
            total_clusters = ntfs.boot_sector.NumberSectors * ntfs.sector_size // ntfs.cluster_size

        report = cls(cluster_size=ntfs.cluster_size, total_clusters=total_clusters)

        if not ntfs.mft:
            raise MftNotAvailableError("Can't build a capacity report without an MFT")

        record_size = ntfs._record_size
        last_segment = ntfs.mft.get(FILE_NUMBER_MFT).size() // record_size
        for segment in range(last_segment):
            try:
                record = ntfs.mft.get(segment)
            except EOFError:
                break
            except Exception:
                if report._is_empty_segment(ntfs, segment):
                    # Pre-allocated but unused MFT slot, not a read error
                    continue
                report.unreadable_segments.append(segment)
                continue
            report.add_record(record)

        try:
            bitmap_record = ntfs.mft.get(FILE_NUMBER_BITMAP)
            report.set_bitmap(bitmap_record.open().read())
        except Exception:
            # No (readable) $Bitmap, free space and bitmap inconsistencies cannot be determined
            pass

        if report.total_clusters is None and report.bitmap is not None:
            report.total_clusters = len(report.bitmap) * 8

        report.finalize()
        return report

    @property
    def complete(self) -> bool:
        """Return whether the report is based on complete data.

        Only when complete can the reported free space be considered exact; partial
        data (unreadable/incomplete records or a missing ``$Bitmap``) means unclaimed
        clusters cannot be told apart from clusters owned by unparsed records.
        """
        return not (self.unreadable_segments or self.incomplete_segments) and self.bitmap is not None

    def add_record(self, record: MftRecord) -> None:
        """Add the stream allocations of a single MFT record to the report.

        Records that are not in use and extension records (reached through the base
        record's ``$ATTRIBUTE_LIST``) are skipped.
        """
        if not record.header.Flags & FILE_RECORD_SEGMENT_IN_USE:
            self.deleted_segments += 1
            return

        if segment_reference(record.header.BaseFileRecordSegment) != 0:
            # Extension record, its attributes are accounted for through the base record
            self.extension_segments += 1
            return

        try:
            attributes = record.attributes
            for collection in attributes.values():
                for attr in collection:
                    self._add_attribute(record, attr)
        except Exception:
            self.incomplete_segments.append(record.segment)

    def set_bitmap(self, data: bytes) -> None:
        """Set the raw volume allocation bitmap (``$Bitmap`` data)."""
        self.bitmap = data

    def finalize(self) -> None:
        """Compute the derived report values: physical union, conflicts and bitmap comparison."""
        self.physical_union = IntervalSet((claim.lcn, claim.end) for claim in self.claims)
        self.conflicts = self._find_conflicts()

        if self.bitmap is not None:
            self.bitmap_allocated = self._bitmap_intervals()
            self.claimed_not_allocated = self.physical_union.difference(self.bitmap_allocated)
            self.allocated_not_claimed = self.bitmap_allocated.difference(self.physical_union)

            if self.total_clusters is not None:
                everything = IntervalSet([(0, self.total_clusters)])
                self.free = everything.difference(self.bitmap_allocated)

    def lookup(self, lcn: int, length: int = 1) -> RangeLookup:
        """Look up a physical cluster range.

        Returns all stream claims covering the range (tracing back to the original
        attribute), any conflicted sub-ranges and the exact ``$Bitmap`` positions.
        """
        end = lcn + length
        claims = [claim for claim in self.claims if claim.lcn < end and claim.end > lcn]
        conflicts = [conflict for conflict in self.conflicts if conflict.lcn < end and conflict.end > lcn]

        bitmap = None
        if self.bitmap is not None:
            bitmap = [self._bitmap_position(cluster) for cluster in range(lcn, end)]

        return RangeLookup(lcn=lcn, length=length, claims=claims, conflicts=conflicts, bitmap=bitmap)

    def summary(self) -> dict:
        """Return a dictionary with the cluster and byte totals of this report."""
        physical_union_clusters = self.physical_union.total()
        return {
            "cluster_size": self.cluster_size,
            "total_clusters": self.total_clusters,
            "complete": self.complete,
            "streams": len(self.streams),
            "logical_bytes": sum(stream.logical_size for stream in self.streams),
            "valid_data_bytes": sum(stream.valid_data_length for stream in self.streams),
            "declared_allocated_bytes": sum(stream.allocated_size for stream in self.streams),
            "stream_claimed_clusters": sum(stream.physical_clusters for stream in self.streams),
            "physical_union_clusters": physical_union_clusters,
            "physical_union_bytes": physical_union_clusters * self.cluster_size,
            "conflict_clusters": sum(conflict.length for conflict in self.conflicts),
            "conflicts": len(self.conflicts),
            "bitmap_allocated_clusters": self.bitmap_allocated.total() if self.bitmap_allocated else None,
            "claimed_not_allocated_clusters": self.claimed_not_allocated.total(),
            "allocated_not_claimed_clusters": self.allocated_not_claimed.total(),
            "free_clusters": self.free.total() if self.free is not None else None,
            "unreadable_segments": len(self.unreadable_segments),
            "incomplete_segments": len(self.incomplete_segments),
        }

    def render(self) -> str:
        """Render a human-readable text version of this report."""
        lines = ["NTFS capacity report", "===================="]
        lines.append(f"Cluster size: {self.cluster_size} bytes")
        if self.total_clusters is not None:
            total_bytes = self.total_clusters * self.cluster_size
            lines.append(f"Volume size: {self.total_clusters} clusters ({total_bytes} bytes)")
        else:
            lines.append("Volume size: unknown")

        summary = self.summary()
        lines.append("")
        lines.append(f"Streams: {summary['streams']}")
        lines.append(f"Logical stream length: {summary['logical_bytes']} bytes")
        lines.append(f"Valid data length: {summary['valid_data_bytes']} bytes")
        lines.append(f"Stream declared allocation: {summary['declared_allocated_bytes']} bytes")
        lines.append(
            f"Physical clusters: {summary['physical_union_clusters']} claimed "
            f"({summary['stream_claimed_clusters']} including duplicate claims)"
        )

        lines.append("")
        if self.conflicts:
            lines.append(f"Conflicting claims ({len(self.conflicts)} ranges):")
            for conflict in self.conflicts:
                owners = ", ".join(
                    f"segment {claim.segment} ${claim.attribute_type.name}:{claim.name or '<default>'}"
                    for claim in conflict.claims
                )
                lines.append(f"  LCN {conflict.lcn}-{conflict.end - 1} ({conflict.length} clusters): {owners}")
        else:
            lines.append("Conflicting claims: none")

        if self.bitmap is None:
            lines.append("")
            lines.append("$Bitmap unavailable: no bitmap comparison, free space unknown")
        else:
            lines.append("")
            lines.append(f"Bitmap allocated: {summary['bitmap_allocated_clusters']} clusters")
            lines.append(
                f"Claimed but free in bitmap: {summary['claimed_not_allocated_clusters']} clusters"
                f"{self._format_ranges(self.claimed_not_allocated)}"
            )
            lines.append(
                f"Allocated but unclaimed (unknown ownership): {summary['allocated_not_claimed_clusters']} clusters"
                f"{self._format_ranges(self.allocated_not_claimed)}"
            )
            if self.free is not None:
                qualifier = "" if self.complete else " (lower bound, partial data)"
                lines.append(f"Free: {summary['free_clusters']} clusters{qualifier}")
            else:
                lines.append("Free: unknown (volume size unknown)")

        if self.unreadable_segments or self.incomplete_segments:
            lines.append("")
            lines.append(
                f"Warning: {len(self.unreadable_segments)} unreadable and {len(self.incomplete_segments)}"
                " incomplete MFT records; exact volume-wide free space cannot be derived"
            )

        return "\n".join(lines)

    @staticmethod
    def _is_empty_segment(ntfs: NTFS, segment: int) -> bool:
        """Return whether the given MFT segment consists of only zero bytes (an unused slot)."""
        try:
            ntfs.mft.fh.seek(segment * ntfs._record_size)
            data = ntfs.mft.fh.read(ntfs._record_size)
        except Exception:
            return False
        return bool(data) and not any(data)

    def _add_attribute(self, record: MftRecord, attr: Attribute) -> None:
        header = attr.header
        if header.resident and header.type != ATTRIBUTE_TYPE_CODE.DATA:
            # Resident metadata attributes occupy no clusters and are not file streams
            return

        key = (record.segment, header.type.value, header.name)
        stream = self._stream_map.get(key)
        if stream is None:
            stream = StreamAllocation(segment=record.segment, attribute_type=header.type, name=header.name)
            self._stream_map[key] = stream
            self.streams.append(stream)

        if header.resident:
            stream.resident = True
            stream.logical_size = max(stream.logical_size, header.size)
            stream.valid_data_length = max(stream.valid_data_length, header.size)
            return

        stream.compressed = stream.compressed or bool(header.flags & ATTRIBUTE_FLAG_COMPRESSION_MASK)
        stream.sparse = stream.sparse or bool(header.flags & ATTRIBUTE_FLAG_SPARSE)
        stream.compression_unit = header.compression_unit or 0

        if not stream.sized or header.lowest_vcn == 0:
            # Sizes are authoritative on the extent starting at VCN 0
            stream.logical_size = header.size
            stream.valid_data_length = header.valid_data_length or 0
            stream.allocated_size = header.allocated_size or 0
            stream.sized = True

        vcn = header.lowest_vcn
        instance = header.header.Instance
        for run_offset, run_size in header.dataruns():
            if run_offset is None:
                # Sparse run (e.g. inside a compression unit or a sparse tail): no physical clusters
                stream.sparse_clusters += run_size
            else:
                claim = ClusterClaim(
                    segment=record.segment,
                    attribute_type=header.type,
                    name=header.name,
                    instance=instance,
                    vcn=vcn,
                    lcn=run_offset,
                    length=run_size,
                    compressed=stream.compressed,
                )
                stream.claims.append(claim)
                self.claims.append(claim)
            vcn += run_size

    def _find_conflicts(self) -> list[ConflictRange]:
        events = []
        for idx, claim in enumerate(self.claims):
            events.append((claim.lcn, idx))
            events.append((claim.end, idx))
        events.sort()

        # Each claim has exactly one start and one end event, so membership can be toggled
        ranges = []
        active: set[int] = set()
        previous = None
        for position, idx in events:
            if previous is not None and position > previous and len(active) >= 2:
                key = frozenset(active)
                if ranges and ranges[-1][2] == key and ranges[-1][1] == previous:
                    ranges[-1][1] = position
                else:
                    ranges.append([previous, position, key])
            if idx in active:
                active.discard(idx)
            else:
                active.add(idx)
            previous = position

        return [
            ConflictRange(lcn=start, length=end - start, claims=tuple(self.claims[idx] for idx in sorted(key)))
            for start, end, key in ranges
        ]

    def _bitmap_intervals(self) -> IntervalSet:
        num_clusters = len(self.bitmap) * 8
        if self.total_clusters is not None:
            num_clusters = min(num_clusters, self.total_clusters)

        ranges = IntervalSet()
        run_start = None
        cluster = 0
        for byte in self.bitmap:
            if cluster >= num_clusters:
                break
            if byte == 0xFF and cluster + 8 <= num_clusters:
                if run_start is None:
                    run_start = cluster
            elif byte == 0x00 and cluster + 8 <= num_clusters:
                if run_start is not None:
                    ranges.add(run_start, cluster)
                    run_start = None
            else:
                for bit in range(8):
                    if cluster + bit >= num_clusters:
                        break
                    if byte >> bit & 1:
                        if run_start is None:
                            run_start = cluster + bit
                    elif run_start is not None:
                        ranges.add(run_start, cluster + bit)
                        run_start = None
            cluster += 8
        if run_start is not None:
            ranges.add(run_start, num_clusters)
        return ranges

    def _bitmap_position(self, cluster: int) -> BitmapPosition:
        byte_offset, bit_offset = divmod(cluster, 8)
        allocated = byte_offset < len(self.bitmap) and bool(self.bitmap[byte_offset] >> bit_offset & 1)
        return BitmapPosition(lcn=cluster, allocated=allocated, byte_offset=byte_offset, bit_offset=bit_offset)

    @staticmethod
    def _format_ranges(ranges: IntervalSet, limit: int = 8) -> str:
        parts = [f"LCN {start}-{end - 1}" if end - start > 1 else f"LCN {start}" for start, end in ranges]
        if len(parts) > limit:
            parts = [*parts[:limit], "..."]
        return f" [{', '.join(parts)}]" if parts else ""
