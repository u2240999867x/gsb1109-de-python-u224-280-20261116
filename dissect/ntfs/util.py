from __future__ import annotations

import struct
from collections import UserDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

from dissect.cstruct import Enum
from dissect.util.stream import RunlistStream

from dissect.ntfs.c_ntfs import (
    ATTRIBUTE_FLAG_COMPRESSION_MASK,
    ATTRIBUTE_FLAG_SPARSE,
    ATTRIBUTE_TYPE_CODE,
    FILE_NUMBER_ROOT,
    SECTOR_SHIFT,
    SECTOR_SIZE,
)
from dissect.ntfs.exceptions import (
    DataRunError,
    FilenameNotAvailableError,
    VolumeNotAvailableError,
)
from dissect.ntfs.stream import CompressedRunlistStream

if TYPE_CHECKING:
    from dissect.ntfs.attr import Attribute
    from dissect.ntfs.c_ntfs import (
        c_ntfs,
    )
    from dissect.ntfs.mft import Mft
    from dissect.ntfs.ntfs import NTFS


@dataclass(slots=True)
class DataExtent:
    """A single extent of a logical data stream.

    Each extent covers the half-open VCN range ``[start_vcn, end_vcn)`` of the stream. An extent backed
    by a real datarun carries its starting ``lcn`` and ``cluster_count``; a sparse extent (mapping pair
    with an offset length of zero) has ``lcn`` set to ``None`` and is backed by zero bytes on read.

    A gap extent (``gap`` is ``True``) marks VCNs for which no attribute extent is available, typically
    because the extension MFT record that should hold them is missing. Gaps are preserved as forensic
    evidence and are never silently zero-filled: building a runlist or opening the stream raises a
    :class:`DataRunError` while a gap is present. Note that a gap is distinct from a sparse extent.

    Extents originate from either a resident attribute (``resident`` is then ``True`` and no VCN/LCN
    information applies) or a non-resident attribute, possibly living in an extension MFT record reached
    through ``$ATTRIBUTE_LIST``. The originating attribute is kept in ``attribute`` for provenance.

    Args:
        start_vcn: The first VCN covered by this extent.
        end_vcn: The VCN one past the last cluster covered by this extent.
        lcn: The starting LCN of the extent, or ``None`` for a sparse or gap extent.
        cluster_count: The number of clusters covered by this extent.
        attribute: The attribute this extent was parsed from.
        resident: Whether this extent is backed by resident attribute data.
        gap: Whether this extent is an unbacked hole due to a missing attribute extent.
    """

    start_vcn: int
    end_vcn: int
    lcn: int | None
    cluster_count: int
    attribute: Attribute | None = None
    resident: bool = False
    gap: bool = False

    @property
    def sparse(self) -> bool:
        """Return whether this extent is an explicit sparse (zero-filled) extent."""
        return self.lcn is None and not self.resident and not self.gap


class AttributeMap(UserDict):
    """Utility dictionary-like object for interacting with a collection of attributes.

    Allows convenient accessing of attributes added to this collection. For example:
    - Get attributes by name, e.g. ``attributes.DATA`` to get all ``$DATA`` attributes.
    - Get attributes by type code enum or integer, e.g. ``attributes[0x80]`` or ``attributes[ATTRIBUTE_TYPE_CODE.DATA]``.
    - Check attribute membership by enum or integer, e.g. ``0x80 in attributes`` or ``ATTRIBUTE_TYPE_CODE.DATA in attributes``.
    - Find all attributes with a given name and type, e.g. ``attributes.find("$I30", ATTRIBUTE_TYPE_CODE.INDEX_ROOT)``.

    Note that any data retrieval from an ``AttributeMap`` will always succeed and return an
    :class:`~dissect.ntfs.util.AttributeCollection`, either empty or containing one or more attributes.
    """  # noqa: E501

    def __getattr__(self, attr: str) -> AttributeCollection:
        if attr in ATTRIBUTE_TYPE_CODE.__members__:
            return self[ATTRIBUTE_TYPE_CODE[attr]]

        return super().__getattribute__(attr)

    def __getitem__(self, item: ATTRIBUTE_TYPE_CODE | int) -> AttributeCollection:
        if isinstance(item, Enum):
            item = item.value
        return self.data.get(item, AttributeCollection())

    def __contains__(self, key: ATTRIBUTE_TYPE_CODE | int) -> bool:
        if isinstance(key, Enum):
            key = key.value
        return super().__contains__(key)

    def add(self, attr: Attribute) -> None:
        """Add an attribute to the collection.

        Note that this is the only intended way to modify the :class:`AttributeMap`!

        Args:
            attr: The attribute to add.
        """
        key = attr.header.type.value
        if key not in self:
            self[key] = AttributeCollection()

        self[key].append(attr)

    def find(self, name: str, attr_type: ATTRIBUTE_TYPE_CODE) -> AttributeCollection:
        """Find attributes by name and attribute type.

        Args:
            name: The name of the attribute to find, usually ``""``.
            attr_type: The attribute type to find.
        """
        name = name.lower()
        return AttributeCollection(
            [attr for attr in self.get(attr_type, AttributeCollection()) if attr.header.name.lower() == name]
        )


class AttributeCollection(list):
    """Utility list-like object for interacting with a list of attributes.

    Allows convenient access to attribute properties for a list of one or more attributes.

    For example, if we have only one attribute we want to access the ``size``, we want to be able
    to do ``attribute_list.size`` instead of ``attribute_list[0].size``.

    Additionally, we can also provide functionality here that we want to perform on a group of
    attributes, like ``open()`` and ``size()``.
    """

    def __getattr__(self, attr: str) -> Any:
        if len(self) == 0:
            raise AttributeError("Attribute not found")

        if hasattr(self[0], attr):
            return getattr(self[0], attr)

        return super().__getattribute__(attr)

    def open(self, allocated: bool = False) -> BinaryIO:
        """Open the data streams on a list of attributes, resident or non-resident.

        Args:
            allocated: Use the actual stream size or the allocated stream size (i.e. include slack space or not).

        Returns:
            A file-like object for the data of this list of attributes.
        """
        if self.header.resident:
            return self.header.open()

        attrs = self._get_stream_attrs()
        # len(attrs) is always >= 1, because if we were empty we would've errored already, and if we have resident
        # attributes, we wouldn't reach this path
        ntfs = attrs[0].record.ntfs

        ensure_volume(ntfs)

        runs = self._get_dataruns(attrs)
        size = attrs[0].header.allocated_size if allocated else attrs[0].header.size

        if attrs[0].header.flags & ATTRIBUTE_FLAG_COMPRESSION_MASK:
            return CompressedRunlistStream(
                ntfs.fh,
                runs,
                size,
                ntfs.cluster_size,
                attrs[0].header.compression_unit,
            )

        return RunlistStream(
            ntfs.fh,
            runs,
            size,
            ntfs.cluster_size,
        )

    def size(self, allocated: bool = False) -> int:
        """Retrieve the data stream size for this list of attributes.

        Args:
            allocated: Return the actual stream size or the allocated stream size (i.e. include slack space or not).

        Returns:
            The requested stream size.
        """
        if self.header.resident:
            return self.header.size

        attrs = self._get_stream_attrs()
        return attrs[0].header.allocated_size if allocated else attrs[0].header.size

    def dataruns(self) -> list[tuple[int, int]]:
        """Get the dataruns for this list of attributes.

        The runlist is only produced after validating that every non-resident attribute extent links
        contiguously through its lowest/highest VCN and that no extension record is missing. Sparse runs
        (``lcn`` is ``None``) originate exclusively from explicit sparse mapping pairs, while a VCN gap
        caused by a missing extension record raises instead of being converted into a run.

        Raises:
            TypeError: If attribute is resident.
            DataRunError: If the attribute extents contain a gap, overlap or inconsistent metadata.
        """
        if self.header.resident:
            raise TypeError("Attribute is resident and has no dataruns")

        return self._get_dataruns()

    def extents(self) -> list[DataExtent]:
        """Return the logical extents that make up this data stream.

        Resident streams yield a single resident :class:`DataExtent`. Non-resident streams yield one
        extent per datarun, ordered by VCN, each annotated with the attribute it was parsed from (which
        may live in an extension MFT record). Attribute type/name, flags, compression unit, lowest VCN,
        highest VCN and the declared file/allocated lengths are cross-checked while stitching.

        A missing extension record does not erase data: the uncovered VCN range is preserved as a gap
        extent (``DataExtent.gap`` is ``True``), keeping the hole as forensic evidence. Gaps are never
        silently zero-filled and are distinct from explicit sparse extents; callers that need a complete
        readable stream should use :meth:`dataruns` or :meth:`open`, which raise :class:`DataRunError`
        while a gap is present.

        Raises:
            DataRunError: If the stream extents are structurally inconsistent (overlap, bad VCN linkage
                or inconsistent metadata).
        """
        if self.header.resident:
            return [DataExtent(0, 0, None, 0, attribute=self[0], resident=True)]

        return self._extents_for(self._get_stream_attrs())

    def gaps(self) -> list[DataExtent]:
        """Return the gap extents caused by missing attribute extension records."""
        return [extent for extent in self.extents() if extent.gap]

    def _get_stream_attrs(self) -> list[Attribute]:
        return sorted((attr for attr in self if not attr.header.resident), key=lambda attr: attr.header.lowest_vcn)

    def _extents_for(self, attrs: list[Attribute]) -> list[DataExtent]:
        extents = []
        expected_vcn = None
        first_header = attrs[0].header
        declared_size = first_header.size
        declared_allocated = first_header.allocated_size

        for attr in attrs:
            header = attr.header

            # Every fragment of one stream must carry the same attribute identity and storage parameters
            if header.type != first_header.type or header.name != first_header.name:
                raise DataRunError(
                    f"Attribute extent identity mismatch: {first_header.type.name}/{first_header.name!r} "
                    f"vs {header.type.name}/{header.name!r}"
                )
            if header.flags != first_header.flags:
                raise DataRunError(f"Attribute extent flags mismatch: {first_header.flags:#x} vs {header.flags:#x}")
            if header.compression_unit != first_header.compression_unit:
                raise DataRunError(
                    f"Attribute extent compression unit mismatch: {first_header.compression_unit} "
                    f"vs {header.compression_unit}"
                )
            if header.lowest_vcn > header.highest_vcn:
                raise DataRunError(
                    f"Attribute extent has LowestVcn {header.lowest_vcn} greater than HighestVcn {header.highest_vcn}"
                )
            if header.size != declared_size or header.allocated_size != declared_allocated:
                raise DataRunError("Attribute extents declare inconsistent file or allocated lengths")

            if expected_vcn is None:
                # The first fragment of a complete stream always starts at VCN 0; anything else means
                # the base attribute extent (in the owning MFT record) is missing
                if header.lowest_vcn != 0:
                    extents.append(DataExtent(0, header.lowest_vcn, None, header.lowest_vcn, gap=True))
                expected_vcn = header.lowest_vcn

            # The next extent must continue exactly where the previous one ended, otherwise an extension
            # record is missing (gap) or two extents claim the same VCN range (overlap)
            if header.lowest_vcn != expected_vcn:
                if header.lowest_vcn > expected_vcn:
                    # Preserve the hole instead of fabricating zero bytes for the missing extension record
                    gap_length = header.lowest_vcn - expected_vcn
                    extents.append(DataExtent(expected_vcn, header.lowest_vcn, None, gap_length, gap=True))
                    expected_vcn = header.lowest_vcn
                else:
                    raise DataRunError(f"Overlapping attribute extents at VCN {header.lowest_vcn}")

            attr_runs = header.dataruns_vcn()
            if not attr_runs:
                # A non-resident attribute without mapping pairs is only valid for a zero-length stream,
                # in which case NTFS records LowestVcn == HighestVcn == 0
                if header.size != 0 or header.lowest_vcn != 0 or header.highest_vcn != 0:
                    raise DataRunError(
                        f"Non-resident attribute with LowestVcn {header.lowest_vcn} and HighestVcn "
                        f"{header.highest_vcn} has no mapping pairs"
                    )
                continue

            for vcn, lcn, cluster_count in attr_runs:
                if vcn != expected_vcn:
                    raise DataRunError(f"Mapping pair VCN {vcn} does not continue stream at VCN {expected_vcn}")
                extents.append(DataExtent(vcn, vcn + cluster_count, lcn, cluster_count, attribute=attr))
                expected_vcn = vcn + cluster_count

            # The declared HighestVcn must match the clusters actually described by the mapping pairs
            ending_vcn = expected_vcn - 1
            if ending_vcn != header.highest_vcn:
                raise DataRunError(
                    f"Attribute extent declares HighestVcn {header.highest_vcn} but mapping pairs end at VCN "
                    f"{ending_vcn}"
                )

        self._validate_declared_length(first_header, extents)
        return extents

    def _require_complete(self, extents: list[DataExtent]) -> None:
        for extent in extents:
            if extent.gap:
                raise DataRunError(
                    f"Cannot read stream, missing attribute extent leaves unbacked VCN gap "
                    f"{extent.start_vcn}..{extent.end_vcn - 1}"
                )

    def _validate_declared_length(self, first_header: Any, extents: list[DataExtent]) -> None:
        ntfs = first_header.record.ntfs if first_header.record else None
        declared_size = first_header.size
        declared_allocated = first_header.allocated_size

        if first_header.flags & (ATTRIBUTE_FLAG_COMPRESSION_MASK | ATTRIBUTE_FLAG_SPARSE):
            # Compressed streams store fewer clusters than their logical size; CompressedRunlistStream
            # validates each compression unit while decompressing. Sparse holes reserve no disk space,
            # so for both storage modes FileSize can exceed AllocatedLength
            return

        if declared_allocated is not None and declared_size > declared_allocated:
            raise DataRunError(f"Declared file size {declared_size} exceeds allocated length {declared_allocated}")

        if not ntfs:
            return

        total_clusters = sum(extent.cluster_count for extent in extents if not extent.sparse and not extent.gap)
        allocated_clusters = (declared_allocated + ntfs.cluster_size - 1) // ntfs.cluster_size
        if total_clusters > allocated_clusters:
            # Fewer backed clusters than AllocatedLength implies is legal (e.g. the reserved MFT zone),
            # but mapping pairs can never claim more clusters than the stream has allocated
            raise DataRunError(
                f"Allocated length {declared_allocated} allows {allocated_clusters} clusters but mapping pairs "
                f"claim {total_clusters} clusters"
            )

    def _get_dataruns(self, attrs: list[Attribute] | None = None) -> list[tuple[int, int]]:
        attrs = attrs or self._get_stream_attrs()
        extents = self._extents_for(attrs)
        # Refuse to turn an unbacked hole into bytes: a gap must never be read as zeroes or as a run
        self._require_complete(extents)
        return [(extent.lcn, extent.cluster_count) for extent in extents]


def apply_fixup(data: bytes) -> bytes:
    """Parse and apply fixup data from ``MULTI_SECTOR_HEADER`` to the given bytes.

    Args:
        data: The bytes to fixup

    Returns:
        The fixed up bytes.
    """
    data = bytearray(data)
    view = memoryview(data)

    fixup_offset = struct.unpack("<H", view[4:6])[0]
    fixup_count = len(data) >> SECTOR_SHIFT

    if (
        # The fixup offset must be even
        fixup_offset & 1
        # The end of the fixup array should not exceed SECTOR_SIZE
        or fixup_offset + (fixup_count + 1) * 2 > SECTOR_SIZE
        # Must have at least one fixup
        or fixup_count == 0
        # And the amount of fixups must match the amount of sectors we have data of
        or fixup_count * SECTOR_SIZE > len(data)
    ):
        raise ValueError(f"Fixup offset out of range: {fixup_offset}")

    fixup = view[fixup_offset:]
    sample = fixup[:2]
    fixup = fixup[2:]

    ptr = view[SECTOR_SIZE - 2 :]
    for _ in range(fixup_count):
        if ptr[:2] != sample:
            raise ValueError("Fixup mismatch")

        ptr[:2] = fixup[:2]
        fixup = fixup[2:]
        ptr = ptr[SECTOR_SIZE:]

    return bytes(data)


def ensure_volume(ntfs: NTFS) -> None:
    """Check if a volume is available for reading.

    A volume in this context refers to a disk or other file that contains the raw NTFS data, not contained
    in system files like the ``$MFT``.

    Raises:
        VolumeNotAvailableError: If a volume is not available.
    """
    if not ntfs or not ntfs.fh:
        raise VolumeNotAvailableError


def get_full_path(mft: Mft, name: str, parent: c_ntfs._MFT_SEGMENT_REFERENCE, seen: set[str] | None = None) -> str:
    """Walk up parent file references to construct a full path.

    Args:
        mft: The MFT object to use for looking up file references.
        name: The file name to use.
        parent: The parent reference to start backtracking from.

    Raises:
        FilenameNotAvailableError: If an MFT record has no filename.
    """
    seen = seen or set()

    path = [name]

    while True:
        parent_ref = segment_reference(parent)
        if parent_ref == FILE_NUMBER_ROOT:
            break

        if parent_ref in seen:
            path.append("<recursion>")
            break

        seen.add(parent_ref)

        try:
            record = mft.get(parent_ref)
            if not record.filename:
                raise FilenameNotAvailableError("No filename")  # noqa: TRY301

            if record.header.SequenceNumber != parent.SequenceNumber:
                path.append(f"<broken_reference_0x{parent_ref:x}#{parent.SequenceNumber}>")
                break

            path.append(record.filename)

            parent = record.attributes.FILE_NAME.attr.ParentDirectory
        except Exception:
            path.append(f"<unknown_segment_0x{parent_ref:x}>")
            break

    return "\\".join(path[::-1])


def ts_to_ns(ts: int) -> int:
    """Convert Windows timestamps to nanosecond timestamps."""
    return (ts * 100) - 11644473600000000000


def segment_reference(reference: c_ntfs._MFT_SEGMENT_REFERENCE) -> int:
    """Helper to calculate the complete segment number from a cstruct MFT segment reference.

    Args:
        reference: A cstruct _MFT_SEGMENT_REFERENCE instance to return the complete segment number of.
    """
    return reference.SegmentNumberLowPart | (reference.SegmentNumberHighPart << 32)


def varint(buf: bytes) -> int:
    """Parse variable integers.

    Dataruns in NTFS are stored as a tuple of variable sized integers. The size of each integer is
    stored in the first byte, 4 bits for each integer. This logic can be seen in
    :func:`AttributeHeader.dataruns <dissect.ntfs.attr.AttributeHeader.dataruns>`.

    This function only parses those variable amount of bytes into actual integers. To do that, we
    simply pad the bytes to 8 bytes long and parse it as a signed 64 bit integer. We pad with 0xff
    if the number is negative and 0x00 otherwise.

    Args:
        buf: The byte buffer to parse a varint from.
    """
    if len(buf) < 8:
        buf += (b"\xff" if buf[-1] & 0x80 else b"\x00") * (8 - len(buf))

    return struct.unpack("<q", buf)[0]


def bsf(value: int) -> int:
    """Count the number of trailing zero bits in an integer of a given size.

    Args:
        value: The integer to count trailing zero bits in.
    """
    return (value & -value).bit_length() - 1 if value else 0
