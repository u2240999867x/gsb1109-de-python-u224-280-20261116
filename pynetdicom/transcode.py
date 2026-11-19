"""Transfer syntax conversion (transcoding) for uncompressed DICOM data.

Only the three uncompressed, unencapsulated transfer syntaxes are within the
convertible range:

* *Implicit VR Little Endian* (``1.2.840.10008.1.2``)
* *Explicit VR Little Endian* (``1.2.840.10008.1.2.1``)
* *Explicit VR Big Endian* (``1.2.840.10008.1.2.2``)

Compressed (encapsulated) and deflated transfer syntaxes are rejected. A
conversion always re-encodes the dataset, file meta information and the actual
binary payloads rather than only rewriting the (0002,0010) *Transfer Syntax
UID*. Numeric payloads (``OW``/``OF``/``OD``/``OL``/``OV`` and the pixel and
waveform elements whose numeric unit size depends on the surrounding context)
have their byte order converted according to their real semantics, while plain
byte strings (``OB`` and ``UN``) are never reversed.
"""

from __future__ import annotations

import logging
import os
import tempfile
from array import array
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from pydicom import Dataset, dcmread, dcmwrite
from pydicom.dataelem import DataElement
from pydicom.dataset import FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.tag import BaseTag, Tag
from pydicom.uid import (
    UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)

from pynetdicom.dsutils import decode, encode

if TYPE_CHECKING:
    from pynetdicom.presentation import PresentationContext

LOGGER = logging.getLogger(__name__)

#: The transfer syntaxes that may be converted to and from.
SUPPORTED_TRANSFER_SYNTAXES: tuple[UID, ...] = (
    ImplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
)

#: Numeric unit size (in bytes) for the fixed-size binary value representations.
_VR_UNIT_SIZE: dict[str, int] = {
    "OW": 2,
    "OL": 4,
    "OF": 4,
    "OV": 8,
    "OD": 8,
}

#: Typecode used by :mod:`array` for each numeric unit size.
_UNIT_TYPECODE: dict[int, str] = {2: "H", 4: "I", 8: "Q"}

#: Waveform elements declared as ``OB or OW`` whose numeric unit is given by
#: (5400,1004) *Waveform Bits Allocated*.
_WAVEFORM_TAGS = frozenset(
    (
        Tag(0x54000110),  # ChannelMinimumValue
        Tag(0x54000112),  # ChannelMaximumValue
        Tag(0x5400100A),  # WaveformPaddingValue
        Tag(0x54001010),  # WaveformData
    )
)

#: PET count elements declared as ``OB or OW`` with no separate descriptor.
_PET_COUNT_TAGS = frozenset((Tag(0x00143050), Tag(0x00143070)))

_WAVEFORM_BITS_TAG = Tag(0x54001004)

_PIXEL_DATA_TAG = Tag(0x7FE00010)
_SOP_CLASS_TAG = Tag(0x00080016)
_SOP_INSTANCE_TAG = Tag(0x00080018)
_MEDIA_SOP_CLASS_TAG = Tag(0x00020002)
_MEDIA_SOP_INSTANCE_TAG = Tag(0x00020003)
_TRANSFER_SYNTAX_TAG = Tag(0x00020010)

_VALID_BITS_ALLOCATED = (8, 16, 32, 64)


class TranscodeError(Exception):
    """Base class for transfer syntax conversion failures."""


class UnsupportedTransferSyntaxError(TranscodeError):
    """The source or target transfer syntax is not in the convertible range."""


class EncapsulatedDatasetError(TranscodeError):
    """The dataset contains encapsulated (compressed) payload data."""


class UnverifiableDatasetError(TranscodeError):
    """The re-encoded copy could not be verified against the original."""


@dataclass(frozen=True)
class TranscodeReport:
    """Outcome of a single conversion attempt.

    Attributes
    ----------
    target_transfer_syntax : pydicom.uid.UID
        The negotiated transfer syntax the copy was encoded with.
    success : bool
        ``True`` when a converted copy was produced and read back without
        error.
    verified : bool
        ``True`` when every element could be authenticated and its value was
        shown to match the original. Copies that retain unauthenticatable
        ``UN`` elements are produced (``success=True``) but never verified.
    reason : str or None
        The rejection reason when ``success`` is ``False``.
    unverified_tags : tuple[pydicom.tag.BaseTag, ...]
        The tags retained in the copy whose values could not be authenticated.
    source : str or None
        The source location, if a file was converted.
    destination : str or None
        The copy location, if a file was converted.
    """

    target_transfer_syntax: UID
    success: bool
    verified: bool
    reason: str | None = None
    unverified_tags: tuple[BaseTag, ...] = ()
    source: str | None = None
    destination: str | None = None


class TranscodedMessage(NamedTuple):
    """An in-memory conversion of a DIMSE message dataset.

    Attributes
    ----------
    data : bytes
        The dataset encoded using the target transfer syntax.
    unverified_tags : tuple[pydicom.tag.BaseTag, ...]
        The tags whose values could not be authenticated.
    """

    data: bytes
    unverified_tags: tuple[BaseTag, ...]


def _as_uid(transfer_syntax: str | UID) -> UID:
    """Return `transfer_syntax` as a :class:`~pydicom.uid.UID`."""
    if isinstance(transfer_syntax, UID):
        return transfer_syntax

    return UID(str(transfer_syntax))


def _validate_syntax(transfer_syntax: UID, role: str) -> None:
    """Reject transfer syntaxes outside of the convertible range."""
    if transfer_syntax not in SUPPORTED_TRANSFER_SYNTAXES:
        raise UnsupportedTransferSyntaxError(
            f"{role} transfer syntax '{transfer_syntax}' is not one of the "
            "supported uncompressed syntaxes (Implicit VR Little Endian, "
            "Explicit VR Little Endian or Explicit VR Big Endian); compressed "
            "and deflated transfer syntaxes are not supported"
        )


def _source_transfer_syntax(
    ds: Dataset, source_transfer_syntax: str | UID | None
) -> UID:
    """Return the declared source transfer syntax for `ds`."""
    if source_transfer_syntax is not None:
        uid = _as_uid(source_transfer_syntax)
        _validate_syntax(uid, "Source")
        return uid

    file_meta = getattr(ds, "file_meta", None)
    if file_meta is not None and _TRANSFER_SYNTAX_TAG in file_meta:
        uid = _as_uid(file_meta[_TRANSFER_SYNTAX_TAG].value)
        _validate_syntax(uid, "Source")
        return uid

    raise TranscodeError(
        "Unable to determine the source transfer syntax: the dataset has no "
        "(0002,0010) Transfer Syntax UID and none was supplied"
    )


def _check_negotiation(
    context: "PresentationContext | None",
    target: UID,
    ds: Dataset,
) -> None:
    """Cross-check the requested conversion against the negotiation."""
    if context is None:
        return

    if not context.transfer_syntax:
        raise TranscodeError("The presentation context has no accepted syntax")

    negotiated = _as_uid(context.transfer_syntax[0])
    if negotiated != target:
        raise UnsupportedTransferSyntaxError(
            f"Target transfer syntax '{target}' was not negotiated; the "
            f"accepted syntax is '{negotiated}'"
        )

    abstract = context.abstract_syntax
    if abstract is not None and _SOP_CLASS_TAG in ds:
        sop_class = _as_uid(ds[_SOP_CLASS_TAG].value)
        if _as_uid(abstract) != sop_class:
            raise TranscodeError(
                f"Negotiated abstract syntax '{abstract}' does not match the "
                f"dataset SOP Class UID '{sop_class}'"
            )


def _iter_elements(ds: Dataset) -> Iterable[tuple[DataElement, Dataset]]:
    """Yield every element of `ds`, recursing into sequence items."""
    for elem in ds:
        yield elem, ds
        if elem.VR == "SQ":
            for item in elem.value:
                yield from _iter_elements(item)


def _numeric_unit_size(elem: DataElement, parent: Dataset) -> int | None:
    """Return the numeric unit size (bytes) for a binary element.

    Returns ``None`` for ``UN`` elements, whose values cannot be authenticated
    and must be preserved as plain byte strings. A unit size of ``1`` means the
    value is a plain byte string that must not be byte swapped.
    """
    tag = elem.tag
    vr = elem.VR

    if tag == _PIXEL_DATA_TAG:
        # Native Pixel Data may use OB, OW, OL or OV depending on the bit
        # depth; the numeric unit is always given by Bits Allocated.
        if vr not in ("OB", "OW", "OL", "OV"):
            raise TranscodeError(
                f"Pixel Data has unexpected VR '{vr}' for native encoding"
            )

        bits_allocated = getattr(parent, "BitsAllocated", None)
        if bits_allocated is None:
            raise TranscodeError(
                "Pixel Data is present but (0028,0100) Bits Allocated is missing"
            )

        if bits_allocated not in _VALID_BITS_ALLOCATED:
            raise TranscodeError(
                f"Unsupported (0028,0100) Bits Allocated value {bits_allocated}"
            )

        return int(bits_allocated) // 8

    if vr in _VR_UNIT_SIZE:
        return _VR_UNIT_SIZE[vr]

    if vr == "OB":
        return _ob_unit_size(Tag(tag), parent)

    if vr == "UN":
        return None

    return 1


def _ob_unit_size(tag: BaseTag, parent: Dataset) -> int:
    """Return the numeric unit size for an ``OB or OW`` element encoded as OB.

    The unit depends on the element's real responsibility: waveform elements
    follow (5400,1004) *Waveform Bits Allocated*, PET count elements are
    16-bit words and every other ``OB`` value is a plain byte string.
    """
    if tag in _WAVEFORM_TAGS:
        if _WAVEFORM_BITS_TAG not in parent:
            raise TranscodeError(
                f"{tag} is present but (5400,1004) Waveform Bits Allocated "
                "is missing"
            )

        bits_allocated = int(parent[_WAVEFORM_BITS_TAG].value)
        if bits_allocated not in _VALID_BITS_ALLOCATED:
            raise TranscodeError(
                "Unsupported (5400,1004) Waveform Bits Allocated value "
                f"{bits_allocated}"
            )

        return bits_allocated // 8

    if tag in _PET_COUNT_TAGS:
        # Dark Current Counts and Air Counts are 16-bit unsigned words.
        return 2

    return 1


def _byte_swap(payload: bytes, unit: int) -> bytes:
    """Reverse the byte order of each `unit`-byte numeric value."""
    if unit == 1 or not payload:
        return payload

    if len(payload) % unit != 0:
        raise TranscodeError(
            f"Binary payload of length {len(payload)} is not aligned to "
            f"{unit}-byte numeric units"
        )

    values = array(_UNIT_TYPECODE[unit], payload)
    values.byteswap()
    return values.tobytes()


def _normalise_little_endian(payload: bytes, unit: int, is_little_endian: bool) -> bytes:
    """Return `payload` with numeric values normalised to little endian."""
    if is_little_endian or unit <= 1:
        return payload

    return _byte_swap(payload, unit)


def _binary_payloads_equal(
    source_payload: bytes,
    converted_payload: bytes,
    unit: int,
    original_is_little_endian: bool,
    converted_is_little_endian: bool,
) -> bool:
    """Compare two binary payloads, tolerating a single DICOM padding byte.

    Numeric values are compared in a common byte order. A trailing zero
    padding byte added to make an odd-length payload even is accepted.
    """
    source = _normalise_little_endian(
        source_payload, unit, original_is_little_endian
    )
    converted = _normalise_little_endian(
        converted_payload, unit, converted_is_little_endian
    )

    if source == converted:
        return True

    if len(source) + 1 == len(converted) and converted[-1] == 0:
        return source == converted[:-1]

    if len(converted) + 1 == len(source) and source[-1] == 0:
        return converted == source[:-1]

    return False


def _prepare_payloads(
    ds: Dataset, source_is_little_endian: bool, target_is_little_endian: bool
) -> list[BaseTag]:
    """Convert binary payloads in `ds` in place for the target byte order.

    Returns the tags of ``UN`` elements that are retained but cannot be
    authenticated.
    """
    unverified: list[BaseTag] = []
    needs_swap = source_is_little_endian != target_is_little_endian

    for elem, parent in _iter_elements(ds):
        if elem.VR == "SQ":
            continue

        # Only the byte/word value representations carry payloads whose byte
        # order has to be converted explicitly; every other VR (including
        # numeric elements such as US/SS/FL/FD and text VRs) is encoded by
        # pydicom according to the target endianness.
        if elem.VR not in ("OB", "OW", "OF", "OD", "OL", "OV", "UN"):
            continue

        if elem.is_undefined_length:
            if elem.tag == _PIXEL_DATA_TAG:
                raise EncapsulatedDatasetError(
                    "Encapsulated (compressed) Pixel Data cannot be transcoded "
                    "between uncompressed transfer syntaxes"
                )

            unverified.append(Tag(elem.tag))
            continue

        unit = _numeric_unit_size(elem, parent)
        if unit is None:
            # Unknown private implicit VR elements (and any other UN payload)
            # are retained byte-for-byte; their values cannot be authenticated.
            unverified.append(Tag(elem.tag))
            continue

        if not isinstance(elem.value, bytes):
            raise TranscodeError(
                f"Expected a binary payload for {elem.tag} {elem.VR}, got "
                f"{type(elem.value).__name__}"
            )

        if needs_swap and unit > 1:
            elem.value = _byte_swap(elem.value, unit)

    return unverified


def _update_file_meta(ds: Dataset, target: UID) -> None:
    """Rewrite the File Meta Information for the target transfer syntax."""
    file_meta = getattr(ds, "file_meta", None)
    if file_meta is None:
        file_meta = FileMetaDataset()
        ds.file_meta = file_meta

    if _SOP_CLASS_TAG in ds and _MEDIA_SOP_CLASS_TAG not in file_meta:
        file_meta.MediaStorageSOPClassUID = _as_uid(ds[_SOP_CLASS_TAG].value)

    if _SOP_INSTANCE_TAG in ds and _MEDIA_SOP_INSTANCE_TAG not in file_meta:
        file_meta.MediaStorageSOPInstanceUID = _as_uid(ds[_SOP_INSTANCE_TAG].value)

    file_meta.TransferSyntaxUID = target
    # File Meta Information is always Explicit VR Little Endian.
    file_meta.set_original_encoding(False, True)


def _compare_datasets(
    original: Dataset,
    converted: Dataset,
    original_is_little_endian: bool,
    converted_is_little_endian: bool,
) -> None:
    """Authenticate that `converted` carries the same values as `original`."""
    original_tags = [elem.tag for elem in original]
    converted_tags = [elem.tag for elem in converted]
    if original_tags != converted_tags:
        raise UnverifiableDatasetError(
            f"Tag list differs after conversion: {original_tags} vs {converted_tags}"
        )

    for source_elem in original:
        target_elem = converted[source_elem.tag]

        if source_elem.VR == "SQ" or target_elem.VR == "SQ":
            if source_elem.VR != target_elem.VR:
                raise UnverifiableDatasetError(
                    f"VR mismatch for {source_elem.tag}: {source_elem.VR} vs "
                    f"{target_elem.VR}"
                )

            source_items = source_elem.value
            target_items = target_elem.value
            if len(source_items) != len(target_items):
                raise UnverifiableDatasetError(
                    f"Sequence item count differs for {source_elem.tag}"
                )

            for source_item, target_item in zip(
                source_items, target_items, strict=True
            ):
                _compare_datasets(
                    source_item,
                    target_item,
                    original_is_little_endian,
                    converted_is_little_endian,
                )

            continue

        if isinstance(source_elem.value, bytes):
            unit = _numeric_unit_size(source_elem, original)
            target_unit = _numeric_unit_size(target_elem, converted)
            if unit != target_unit:
                raise UnverifiableDatasetError(
                    f"Binary numeric unit differs for {source_elem.tag}"
                )

            if unit is None:
                # UN payloads are preserved byte-for-byte, never swapped; a
                # trailing padding byte added to an odd-length value is valid.
                if not _binary_payloads_equal(
                    source_elem.value,
                    target_elem.value,
                    1,
                    original_is_little_endian,
                    converted_is_little_endian,
                ):
                    raise UnverifiableDatasetError(
                        f"UN payload differs for {source_elem.tag}"
                    )

                continue

            if not _binary_payloads_equal(
                source_elem.value,
                target_elem.value,
                unit,
                original_is_little_endian,
                converted_is_little_endian,
            ):
                raise UnverifiableDatasetError(
                    f"Binary payload differs for {source_elem.tag} {source_elem.VR}"
                )
        elif source_elem.value != target_elem.value:
            raise UnverifiableDatasetError(
                f"Value differs for {source_elem.tag} {source_elem.VR}: "
                f"{source_elem.value!r} vs {target_elem.value!r}"
            )


def _verify_identity(original: Dataset, converted: Dataset) -> None:
    """Authenticate that the SOP instance identity was preserved.

    The canonical identity is the dataset-level (0008,0016) *SOP Class UID*
    and (0008,0018) *SOP Instance UID*. The File Meta Information of the copy
    must agree with those values, even when the source file meta information
    was itself inconsistent.
    """
    for tag in (_SOP_CLASS_TAG, _SOP_INSTANCE_TAG):
        if tag in original:
            if tag not in converted or original[tag].value != converted[tag].value:
                raise UnverifiableDatasetError(
                    f"Instance identity element {tag} was not preserved"
                )

    converted_meta = getattr(converted, "file_meta", None)
    if converted_meta is not None:
        for meta_tag, dataset_tag in (
            (_MEDIA_SOP_CLASS_TAG, _SOP_CLASS_TAG),
            (_MEDIA_SOP_INSTANCE_TAG, _SOP_INSTANCE_TAG),
        ):
            if meta_tag in converted_meta and dataset_tag in converted:
                if converted_meta[meta_tag].value != converted[dataset_tag].value:
                    raise UnverifiableDatasetError(
                        f"File meta element {meta_tag} does not match the "
                        "dataset SOP identity"
                    )


def transcode_dataset(
    ds: Dataset,
    transfer_syntax: str | UID,
    *,
    source_transfer_syntax: str | UID | None = None,
    context: "PresentationContext | None" = None,
) -> tuple[Dataset, tuple[BaseTag, ...]]:
    """Return a verified copy of `ds` encoded for `transfer_syntax`.

    The input dataset is never modified. The copy goes through a full
    encode/decode cycle and is authenticated against the original before it is
    returned.

    Parameters
    ----------
    ds : pydicom.dataset.Dataset
        The dataset to convert.
    transfer_syntax : str or pydicom.uid.UID
        The target transfer syntax, which must be one of
        :attr:`SUPPORTED_TRANSFER_SYNTAXES`.
    source_transfer_syntax : str or pydicom.uid.UID, optional
        The source transfer syntax. If not supplied it is taken from the
        (0002,0010) element of the dataset file meta information.
    context : pynetdicom.presentation.PresentationContext, optional
        The negotiated presentation context, used to confirm the target syntax
        and abstract syntax are actually in scope.

    Returns
    -------
    tuple[pydicom.dataset.Dataset, tuple[pydicom.tag.BaseTag, ...]]
        The converted dataset and the tags of any retained elements whose
        values could not be authenticated.

    Raises
    ------
    TranscodeError
        If the conversion is not permitted or the copy cannot be verified.
    """
    target = _as_uid(transfer_syntax)
    _validate_syntax(target, "Target")
    source = _source_transfer_syntax(ds, source_transfer_syntax)
    _check_negotiation(context, target, ds)

    work = deepcopy(ds)

    # Rejects encapsulated (compressed) Pixel Data and converts the byte order
    # of every binary payload according to its real numeric semantics.
    unverified = _prepare_payloads(
        work, source.is_little_endian, target.is_little_endian
    )
    _update_file_meta(work, target)
    work.set_original_encoding(target.is_implicit_VR, target.is_little_endian)

    try:
        encoded = encode(work, target.is_implicit_VR, target.is_little_endian)
    except Exception as exc:
        raise TranscodeError(
            f"Encoding the dataset with '{target}' failed: {exc}"
        ) from exc

    if encoded is None:
        raise TranscodeError(f"Encoding the dataset with '{target}' failed")

    try:
        reread = decode(
            BytesIO(encoded), target.is_implicit_VR, target.is_little_endian
        )
    except Exception as exc:
        raise TranscodeError(
            f"Decoding the re-encoded dataset failed: {exc}"
        ) from exc

    _compare_datasets(
        ds, reread, source.is_little_endian, target.is_little_endian
    )
    _verify_identity(ds, reread)

    return work, tuple(unverified)


def transcode_message(
    bytestring: bytes,
    source_transfer_syntax: str | UID,
    transfer_syntax: str | UID,
    *,
    context: "PresentationContext | None" = None,
) -> TranscodedMessage:
    """Convert an encoded DIMSE message dataset between transfer syntaxes.

    Parameters
    ----------
    bytestring : bytes
        The dataset encoded with `source_transfer_syntax`.
    source_transfer_syntax : str or pydicom.uid.UID
        The transfer syntax of `bytestring`.
    transfer_syntax : str or pydicom.uid.UID
        The target transfer syntax.
    context : pynetdicom.presentation.PresentationContext, optional
        The negotiated presentation context.

    Returns
    -------
    TranscodedMessage
        The re-encoded dataset and any unauthenticatable tags.

    Raises
    ------
    TranscodeError
        If the conversion is not permitted or the copy cannot be verified.
    """
    source = _as_uid(source_transfer_syntax)
    target = _as_uid(transfer_syntax)
    _validate_syntax(source, "Source")
    _validate_syntax(target, "Target")

    try:
        ds = decode(
            BytesIO(bytestring), source.is_implicit_VR, source.is_little_endian
        )
    except Exception as exc:
        raise TranscodeError(f"Decoding the source dataset failed: {exc}") from exc

    if ds is None:
        raise TranscodeError("Decoding the source dataset failed")

    work, unverified = transcode_dataset(
        ds,
        target,
        source_transfer_syntax=source,
        context=context,
    )
    try:
        encoded = encode(work, target.is_implicit_VR, target.is_little_endian)
    except Exception as exc:
        raise TranscodeError(
            f"Encoding the dataset with '{target}' failed: {exc}"
        ) from exc

    if encoded is None:
        raise TranscodeError(f"Encoding the dataset with '{target}' failed")

    return TranscodedMessage(encoded, unverified)


def transcode_file(
    source: str | Path,
    destination: str | Path,
    transfer_syntax: str | UID,
    *,
    context: "PresentationContext | None" = None,
) -> TranscodeReport:
    """Write a transfer syntax converted copy of `source` to `destination`.

    The source file is never modified or overwritten and the output is only
    published after it has been written and re-read successfully.

    Parameters
    ----------
    source : str or pathlib.Path
        Path to the source DICOM File.
    destination : str or pathlib.Path
        Path to write the converted copy to. Must not be the source file.
    transfer_syntax : str or pydicom.uid.UID
        The target transfer syntax.
    context : pynetdicom.presentation.PresentationContext, optional
        The negotiated presentation context.

    Returns
    -------
    TranscodeReport
        The outcome of the conversion, including any tags that could not be
        authenticated.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    target = _as_uid(transfer_syntax)
    source_label = str(source_path)
    destination_label = str(destination_path)

    temporary_path: str | None = None
    try:
        if source_path.resolve() == destination_path.resolve():
            raise TranscodeError(
                "The destination must not be the same file as the source"
            )

        ds = dcmread(source_path)
        work, unverified = transcode_dataset(
            ds, target, context=context
        )

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=destination_path.parent, suffix=".part"
        )
        os.close(descriptor)
        dcmwrite(
            temporary_path,
            work,
            implicit_vr=target.is_implicit_VR,
            little_endian=target.is_little_endian,
            enforce_file_format=True,
        )

        written = dcmread(temporary_path)
        if written.file_meta[_TRANSFER_SYNTAX_TAG].value != target:
            raise UnverifiableDatasetError(
                "The written file meta information does not declare the "
                "target transfer syntax"
            )

        if written.original_encoding != (
            target.is_implicit_VR,
            target.is_little_endian,
        ):
            raise UnverifiableDatasetError(
                "The written dataset does not use the negotiated encoding"
            )

        _compare_datasets(
            ds,
            written,
            ds.file_meta[_TRANSFER_SYNTAX_TAG].value.is_little_endian,
            target.is_little_endian,
        )
        _verify_identity(ds, written)

        os.replace(temporary_path, destination_path)
        temporary_path = None

        return TranscodeReport(
            target_transfer_syntax=target,
            success=True,
            verified=not unverified,
            unverified_tags=unverified,
            source=source_label,
            destination=destination_label,
        )
    except (TranscodeError, OSError, InvalidDicomError) as exc:
        LOGGER.warning("Transcoding %s failed: %s", source_label, exc)
        return TranscodeReport(
            target_transfer_syntax=target,
            success=False,
            verified=False,
            reason=str(exc),
            source=source_label,
            destination=destination_label,
        )
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except OSError:
                LOGGER.debug("Could not remove temporary file %s", temporary_path)


def transcode_instances(
    requests: Iterable[tuple[str | Path, str | Path]],
    transfer_syntax: str | UID,
    *,
    context: "PresentationContext | None" = None,
) -> list[TranscodeReport]:
    """Convert a batch of ``(source, destination)`` file pairs.

    Returns one :class:`TranscodeReport` per request. Rejected or unverifiable
    instances never appear in :func:`successful_reports`.
    """
    target = _as_uid(transfer_syntax)
    return [
        transcode_file(source, destination, target, context=context)
        for source, destination in requests
    ]


def successful_reports(reports: Iterable[TranscodeReport]) -> list[TranscodeReport]:
    """Return the fully authenticated conversion reports (the success list).

    Copies containing unauthenticatable retained elements are excluded even
    though the conversion itself succeeded.
    """
    return [report for report in reports if report.success and report.verified]
