"""Unit tests for the transfer syntax conversion utilities."""

from array import array
from pathlib import Path

import pytest
from pydicom import Dataset, dcmread, dcmwrite
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import read_dataset
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000Lossless,
    UID,
)

from pynetdicom.dsutils import create_file_meta, encode
from pynetdicom.presentation import PresentationContext
from pynetdicom.sop_class import CTImageStorage, MRImageStorage
from pynetdicom.transcode import (
    SUPPORTED_TRANSFER_SYNTAXES,
    EncapsulatedDatasetError,
    TranscodeError,
    TranscodedMessage,
    UnverifiableDatasetError,
    UnsupportedTransferSyntaxError,
    successful_reports,
    transcode_dataset,
    transcode_file,
    transcode_instances,
    transcode_message,
)

TEST_DS_DIR = Path(__file__).parent / "dicom_files"
RT_DS = TEST_DS_DIR / "RTImageStorage.dcm"
CT_DS = TEST_DS_DIR / "CTImageStorage.dcm"
MR_BE_DS = TEST_DS_DIR / "MRImageStorage_ExplicitVRBigEndian.dcm"
J2K_DS = TEST_DS_DIR / "MRImageStorage_JPG2000_Lossless.dcm"
DEFLATED_DS = TEST_DS_DIR / "SCImageStorage_Deflated.dcm"

ALL_TARGETS = [
    ImplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
]


def _little_endian_words(payload: bytes, little_endian: bool) -> bytes:
    """Return 16-bit `payload` normalised to little endian word order."""
    if little_endian:
        return payload

    return b"".join(
        payload[index : index + 2][::-1]
        for index in range(0, len(payload), 2)
    )


def _minimal_dataset() -> Dataset:
    """Return a small native dataset with a 16-bit pixel payload."""
    ds = Dataset()
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = "1.2.3.4"
    ds.PatientID = "ABC123"
    ds.Rows = 4
    ds.Columns = 2
    ds.BitsAllocated = 16
    ds.SamplesPerPixel = 1
    ds.add_new(
        0x7FE00010,
        "OW",
        array("H", [0x0102, 0x0304, 0x0506, 0x0708]).tobytes(),
    )
    return ds


class TestSupportedSyntaxes:
    """Test the convertible transfer syntax range."""

    def test_supported_range(self):
        assert SUPPORTED_TRANSFER_SYNTAXES == (
            ImplicitVRLittleEndian,
            ExplicitVRLittleEndian,
            ExplicitVRBigEndian,
        )

    def test_supported_flags(self):
        for uid in SUPPORTED_TRANSFER_SYNTAXES:
            assert uid.is_compressed is False
            assert uid.is_deflated is False


class TestTranscodeDataset:
    """Test transcode_dataset()."""

    @pytest.mark.parametrize("target", ALL_TARGETS)
    @pytest.mark.parametrize(
        "source_path",
        [RT_DS, CT_DS, MR_BE_DS],
        ids=["implicit-le", "explicit-le", "explicit-be"],
    )
    def test_native_fixtures(self, source_path, target):
        """Every native fixture converts to every supported syntax."""
        original = dcmread(source_path)
        converted, unverified = transcode_dataset(original, target)

        assert not unverified
        assert converted.file_meta.TransferSyntaxUID == target
        assert converted.original_encoding == (
            target.is_implicit_VR,
            target.is_little_endian,
        )

        # Identity preserved
        assert converted.SOPClassUID == original.SOPClassUID
        assert converted.SOPInstanceUID == original.SOPInstanceUID

        # Scalar values preserved
        assert converted.Rows == original.Rows
        assert converted.Columns == original.Columns
        assert converted.BitsAllocated == original.BitsAllocated

        # Pixel values preserved at the numeric (16-bit word) level
        original_words = _little_endian_words(
            original.PixelData, original.file_meta.TransferSyntaxUID.is_little_endian
        )
        converted_words = _little_endian_words(
            converted.PixelData, target.is_little_endian
        )
        assert converted_words == original_words

    def test_input_not_modified(self):
        """The source dataset must be left untouched."""
        original = dcmread(CT_DS)
        original_pixels = original.PixelData
        original_ts = original.file_meta.TransferSyntaxUID

        transcode_dataset(original, ExplicitVRBigEndian)

        assert original.PixelData == original_pixels
        assert original.file_meta.TransferSyntaxUID == original_ts

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_file_meta_rewritten(self, target):
        original = dcmread(MR_BE_DS)
        converted, _ = transcode_dataset(original, target)

        assert converted.file_meta.TransferSyntaxUID == target
        assert converted.file_meta.MediaStorageSOPClassUID == original.SOPClassUID
        assert (
            converted.file_meta.MediaStorageSOPInstanceUID
            == original.SOPInstanceUID
        )

    def test_unsupported_target(self):
        original = dcmread(CT_DS)
        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_dataset(original, JPEG2000Lossless)

    def test_deflated_target_rejected(self):
        original = dcmread(CT_DS)
        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_dataset(original, DeflatedExplicitVRLittleEndian)

    def test_unsupported_source(self):
        original = dcmread(J2K_DS)
        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_dataset(original, ExplicitVRLittleEndian)

    def test_deflated_source_rejected(self):
        original = dcmread(DEFLATED_DS)
        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_dataset(original, ExplicitVRLittleEndian)

    def test_encapsulated_pixel_data_rejected(self):
        original = dcmread(J2K_DS)
        # Bypass the transfer syntax guard to hit the payload guard.
        original.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        with pytest.raises(EncapsulatedDatasetError):
            transcode_dataset(original, ExplicitVRBigEndian)

    def test_missing_source_syntax(self):
        ds = _minimal_dataset()
        assert not hasattr(ds, "file_meta")
        with pytest.raises(TranscodeError):
            transcode_dataset(ds, ExplicitVRLittleEndian)

    def test_explicit_source_syntax_argument(self):
        ds = _minimal_dataset()
        converted, unverified = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert not unverified
        assert converted.file_meta.TransferSyntaxUID == ExplicitVRBigEndian

    def test_unknown_private_implicit_vr_retained(self):
        """Unknown private elements decode to UN and are retained unverified."""
        ds = Dataset()
        ds.PatientID = "ABC123"
        ds.Rows = 64
        ds.add_new(0x00990010, "LO", "CREATOR")
        ds.add_new(0x00991001, "US", [1, 2])
        ds.add_new(0x00991002, "LO", "HELLO")

        raw = encode(ds, True, True)
        decoded = read_dataset(DicomBytesIO(raw), True, True)
        assert decoded[0x00991001].VR == "UN"

        converted, unverified = transcode_dataset(
            decoded,
            ExplicitVRLittleEndian,
            source_transfer_syntax=ImplicitVRLittleEndian,
        )

        assert 0x00991001 in [int(tag) for tag in unverified]
        assert 0x00991002 in [int(tag) for tag in unverified]
        # Bytes are preserved verbatim (no numeric byte swap).
        assert converted[0x00991001].value == b"\x01\x00\x02\x00"
        assert converted[0x00991002].value.startswith(b"HELLO")
        # Known elements remain authenticated.
        assert converted.PatientID == "ABC123"
        assert converted.Rows == 64

    def test_private_un_preserved_to_big_endian(self):
        """UN payloads are never byte swapped for big endian output."""
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.add_new(0x00991001, "UN", b"\xaa\xbb\xcc\xdd")

        converted, unverified = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert len(unverified) == 1
        assert converted[0x00991001].value == b"\xaa\xbb\xcc\xdd"

    def test_ob_not_swapped(self):
        """Plain OB byte strings keep their byte order."""
        ds = _minimal_dataset()
        payload = b"\xaa\xbb\xcc"
        ds.add_new(0x00991001, "OB", payload)

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert converted[0x00991001].value.startswith(payload)

    @pytest.mark.parametrize(
        "vr,unit,typecode",
        [
            ("OW", 2, "H"),
            ("OL", 4, "I"),
            ("OF", 4, "f"),
            ("OV", 8, "Q"),
            ("OD", 8, "d"),
        ],
    )
    def test_numeric_binary_vr_byte_swapped(self, vr, unit, typecode):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        if typecode in "fd":
            seed = 1.0
        elif unit == 2:
            seed = 0x0102
        else:
            seed = 0x11223344
        values = array(typecode, [seed])
        little_endian_payload = values.tobytes()
        tag = {
            "OW": 0x00281103,
            "OL": 0x00281101,
            "OF": 0x7FE00008,
            "OV": 0x00281102,
            "OD": 0x7FE00009,
        }[vr]
        ds.add_new(tag, vr, little_endian_payload)

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        expected = little_endian_payload[::-1] if unit == 8 else b"".join(
            little_endian_payload[i : i + unit][::-1]
            for i in range(0, len(little_endian_payload), unit)
        )
        assert converted[tag].value == expected

        # Converting back restores the original bytes.
        restored, _ = transcode_dataset(
            converted,
            ExplicitVRLittleEndian,
            source_transfer_syntax=ExplicitVRBigEndian,
        )
        assert restored[tag].value == little_endian_payload

    @pytest.mark.parametrize(
        "bits,vr,typecode,value",
        [
            (8, "OB", "B", 0x7F),
            (16, "OW", "H", 0x0102),
            (32, "OL", "I", 0x11223344),
            (64, "OV", "Q", 0x1122334455667788),
        ],
    )
    def test_pixel_data_unit_from_bits_allocated(self, bits, vr, typecode, value):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.BitsAllocated = bits
        ds.Rows = 1
        ds.Columns = 1
        ds.SamplesPerPixel = 1
        payload = array(typecode, [value]).tobytes()
        ds.add_new(0x7FE00010, vr, payload)

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )

        if bits == 8:
            assert converted.PixelData == payload
        else:
            assert converted.PixelData == payload[::-1]

    def test_16bit_ob_pixel_data_word_swapped(self):
        """Pixel Data encoded as OB still follows Bits Allocated."""
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.BitsAllocated = 16
        ds.Rows = 1
        ds.Columns = 2
        ds.SamplesPerPixel = 1
        ds.add_new(0x7FE00010, "OB", b"\x01\x00\x02\x00")

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert converted.PixelData == b"\x00\x01\x00\x02"

    def test_waveform_ob_word_swapped(self):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.WaveformBitsAllocated = 16
        ds.add_new(0x54001010, "OB", array("H", [0x0102]).tobytes())

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert converted[0x54001010].value == b"\x01\x02"

    def test_8bit_waveform_ob_not_swapped(self):
        """8-bit waveform data is a byte stream and must not be swapped."""
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.WaveformBitsAllocated = 8
        ds.add_new(0x54001010, "OB", b"\xaa\xbb\xcc\xdd")

        converted, _ = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert converted[0x54001010].value == b"\xaa\xbb\xcc\xdd"

    def test_waveform_missing_descriptor_rejected(self):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.add_new(0x54001010, "OB", b"\x01\x00")

        with pytest.raises(TranscodeError):
            transcode_dataset(
                ds,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
            )

    def test_missing_bits_allocated_rejected(self):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.add_new(0x7FE00010, "OW", b"\x01\x00")

        with pytest.raises(TranscodeError):
            transcode_dataset(
                ds,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
            )

    def test_misaligned_payload_rejected(self):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.BitsAllocated = 16
        ds.add_new(0x7FE00010, "OW", b"\x01\x00\x02")

        with pytest.raises(TranscodeError):
            transcode_dataset(
                ds,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
            )

    def test_odd_length_ob_padding_accepted(self):
        ds = _minimal_dataset()
        ds.add_new(0x00991001, "OB", b"\xaa\xbb\xcc")

        converted, _ = transcode_dataset(
            ds, ExplicitVRLittleEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )
        assert converted[0x00991001].value.startswith(b"\xaa\xbb\xcc")

    def test_nested_sequence_payloads(self):
        item = Dataset()
        item.SOPInstanceUID = "1.2.9"
        item.add_new(0x00283006, "OW", array("H", [0x0102, 0x0304]).tobytes())
        item.add_new(0x00991001, "UN", b"\xaa\xbb")

        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.add_new(0x00081115, "SQ", [item])

        converted, unverified = transcode_dataset(
            ds, ExplicitVRBigEndian, source_transfer_syntax=ExplicitVRLittleEndian
        )

        sequence_item = converted[0x00081115][0]
        assert sequence_item[0x00283006].value == b"\x01\x02\x03\x04"
        assert sequence_item[0x00991001].value.startswith(b"\xaa\xbb")
        assert 0x00991001 in [int(tag) for tag in unverified]

    def test_negotiated_context_accepted(self):
        ds = _minimal_dataset()
        context = PresentationContext()
        context.abstract_syntax = CTImageStorage
        context.add_transfer_syntax(ExplicitVRBigEndian)
        context.context_id = 1

        converted, _ = transcode_dataset(
            ds,
            ExplicitVRBigEndian,
            source_transfer_syntax=ExplicitVRLittleEndian,
            context=context,
        )
        assert converted.file_meta.TransferSyntaxUID == ExplicitVRBigEndian

    def test_context_target_not_negotiated(self):
        ds = _minimal_dataset()
        context = PresentationContext()
        context.abstract_syntax = CTImageStorage
        context.add_transfer_syntax(ExplicitVRLittleEndian)
        context.context_id = 1

        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_dataset(
                ds,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
                context=context,
            )

    def test_context_abstract_syntax_mismatch(self):
        ds = _minimal_dataset()
        context = PresentationContext()
        context.abstract_syntax = MRImageStorage
        context.add_transfer_syntax(ExplicitVRBigEndian)
        context.context_id = 1

        with pytest.raises(TranscodeError):
            transcode_dataset(
                ds,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
                context=context,
            )

    def test_verification_failure_raises(self, monkeypatch):
        """A tampered re-encoding must not be returned as a success."""
        from pynetdicom import transcode as module

        original = _minimal_dataset()

        def broken_compare(*args, **kwargs):
            raise UnverifiableDatasetError("simulated")

        monkeypatch.setattr(module, "_compare_datasets", broken_compare)
        with pytest.raises(UnverifiableDatasetError):
            transcode_dataset(
                original,
                ExplicitVRBigEndian,
                source_transfer_syntax=ExplicitVRLittleEndian,
            )


class TestTranscodeMessage:
    """Test transcode_message()."""

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_message_round_trip(self, target):
        ds = _minimal_dataset()
        raw = encode(ds, False, True)

        message = transcode_message(
            raw, ExplicitVRLittleEndian, target
        )
        assert isinstance(message, TranscodedMessage)
        assert message.unverified_tags == ()

        reread = read_dataset(
            DicomBytesIO(message.data),
            target.is_implicit_VR,
            target.is_little_endian,
        )
        assert reread.PatientID == ds.PatientID
        assert reread.SOPInstanceUID == ds.SOPInstanceUID
        expected_words = _little_endian_words(ds.PixelData, True)
        assert _little_endian_words(reread.PixelData, target.is_little_endian) == (
            expected_words
        )

    def test_message_unsupported_source(self):
        with pytest.raises(UnsupportedTransferSyntaxError):
            transcode_message(
                b"", ExplicitVRLittleEndian, JPEG2000Lossless
            )

    def test_message_preserves_un(self):
        ds = Dataset()
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = "1.2.3.4"
        ds.add_new(0x00991001, "UN", b"\xaa\xbb\xcc\xdd")
        raw = encode(ds, True, True)

        message = transcode_message(
            raw, ImplicitVRLittleEndian, ExplicitVRBigEndian
        )
        assert 0x00991001 in [int(tag) for tag in message.unverified_tags]

        reread = read_dataset(DicomBytesIO(message.data), False, False)
        assert reread[0x00991001].value == b"\xaa\xbb\xcc\xdd"


class TestTranscodeFile:
    """Test transcode_file()."""

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_writes_valid_file(self, tmp_path, target):
        destination = tmp_path / "converted.dcm"
        report = transcode_file(CT_DS, destination, target)

        assert report.success
        assert report.verified
        assert report.reason is None

        written = dcmread(destination)
        assert written.file_meta.TransferSyntaxUID == target
        assert written.original_encoding == (
            target.is_implicit_VR,
            target.is_little_endian,
        )
        # DICOM File Format preamble is present.
        with open(destination, "rb") as fp:
            assert fp.read(132)[128:132] == b"DICM"

    def test_source_file_unchanged(self, tmp_path):
        source_bytes = CT_DS.read_bytes()
        transcode_file(
            CT_DS, tmp_path / "converted.dcm", ExplicitVRBigEndian
        )
        assert CT_DS.read_bytes() == source_bytes

    def test_same_file_rejected(self, tmp_path):
        source = tmp_path / "source.dcm"
        source.write_bytes(CT_DS.read_bytes())

        report = transcode_file(
            source, source, ExplicitVRBigEndian
        )
        assert not report.success
        assert report.verified is False

    def test_compressed_source_report(self, tmp_path):
        report = transcode_file(
            J2K_DS, tmp_path / "converted.dcm", ExplicitVRLittleEndian
        )
        assert not report.success
        assert not report.verified
        assert "supported" in report.reason.lower()
        assert not (tmp_path / "converted.dcm").exists()

    def test_failure_leaves_no_partial_file(self, tmp_path):
        destination = tmp_path / "converted.dcm"
        transcode_file(
            J2K_DS, destination, ExplicitVRLittleEndian
        )
        assert list(tmp_path.iterdir()) == []

    def test_missing_source_returns_report(self, tmp_path):
        report = transcode_file(
            tmp_path / "missing.dcm",
            tmp_path / "converted.dcm",
            ExplicitVRLittleEndian,
        )
        assert not report.success
        assert not report.verified
        assert report.reason
        assert not (tmp_path / "converted.dcm").exists()

    def test_corrupt_source_returns_report(self, tmp_path):
        source = tmp_path / "corrupt.dcm"
        source.write_bytes(b"not a dicom file")
        report = transcode_file(
            source, tmp_path / "converted.dcm", ExplicitVRLittleEndian
        )
        assert not report.success
        assert not report.verified
        assert report.reason
        assert not (tmp_path / "converted.dcm").exists()

    def test_values_match_after_reread(self, tmp_path):
        original = dcmread(CT_DS)
        destination = tmp_path / "converted.dcm"
        transcode_file(CT_DS, destination, ExplicitVRBigEndian)

        written = dcmread(destination)
        original_words = _little_endian_words(
            original.PixelData,
            original.file_meta.TransferSyntaxUID.is_little_endian,
        )
        written_words = _little_endian_words(
            written.PixelData, False
        )
        assert written_words == original_words
        assert written.SOPInstanceUID == original.SOPInstanceUID


class TestBatch:
    """Test transcode_instances() and successful_reports()."""

    def test_batch_reports(self, tmp_path):
        requests = [
            (CT_DS, tmp_path / "ct.dcm"),
            (J2K_DS, tmp_path / "j2k.dcm"),
            (MR_BE_DS, tmp_path / "mr.dcm"),
        ]
        reports = transcode_instances(requests, ExplicitVRBigEndian)

        assert len(reports) == 3
        success = [report for report in reports if report.success]
        failures = [report for report in reports if not report.success]
        assert len(success) == 2
        assert len(failures) == 1

        authenticated = successful_reports(reports)
        assert len(authenticated) == 2
        assert all(report.verified for report in authenticated)

    def test_unverified_copy_excluded_from_success_list(self, tmp_path):
        ds = _minimal_dataset()
        ds.add_new(0x00991001, "UN", b"\xaa\xbb")
        source = tmp_path / "source.dcm"

        # Build a file with the required transfer syntax in the meta info.
        ds.file_meta = create_file_meta(
            sop_class_uid=UID(ds.SOPClassUID),
            sop_instance_uid=UID(ds.SOPInstanceUID),
            transfer_syntax=ExplicitVRLittleEndian,
        )
        dcmwrite(source, ds, enforce_file_format=True)

        reports = transcode_instances(
            [(source, tmp_path / "out.dcm")], ExplicitVRBigEndian
        )
        report = reports[0]
        assert report.success
        assert not report.verified
        assert successful_reports(reports) == []
