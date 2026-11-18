import email
import tempfile
import unittest
from pathlib import Path

from imap_tools import MailMessage
from imap_tools.attachment_filename import (
    INCOMPLETE_MARKER,
    build_filename_info,
    has_filename_parameter,
    safe_filename,
    unique_filename,
)


def part_from_headers(headers: str, body: bytes = b'payload'):
    raw = (headers.strip() + '\n\n').encode('ascii') + body
    return email.message_from_bytes(raw)


def info(headers: str):
    return build_filename_info(part_from_headers(headers))


class FilenamePrecedenceTest(unittest.TestCase):
    def test_plain_parameter(self):
        result = info('Content-Disposition: attachment; filename="plain.txt"')
        self.assertEqual(result.name, 'plain.txt')
        self.assertTrue(result.complete)
        self.assertEqual(result.source, 'content-disposition')
        self.assertEqual(result.parameter, 'filename')
        self.assertEqual(result.raw, b'plain.txt')
        self.assertEqual(result.segments[0].raw, b'plain.txt')

    def test_content_disposition_wins_over_content_type(self):
        result = info(
            'Content-Type: application/octet-stream; name="type-name.txt"\n'
            'Content-Disposition: attachment; filename="disp-name.txt"'
        )
        self.assertEqual(result.name, 'disp-name.txt')
        self.assertEqual(result.parameter, 'filename')
        self.assertEqual([s.parameter for s in result.fallback_segments], ['name'])

    def test_content_type_name_used_when_disposition_absent(self):
        result = info('Content-Type: application/octet-stream; name="type-name.txt"')
        self.assertEqual(result.name, 'type-name.txt')
        self.assertEqual(result.source, 'content-type')
        self.assertEqual(result.parameter, 'name')

    def test_extended_beats_plain(self):
        result = info(
            "Content-Disposition: attachment; filename=fallback.txt; "
            "filename*=utf-8''%E4%B8%AD%E6%96%87.txt"
        )
        self.assertEqual(result.name, '中文.txt')
        self.assertTrue(result.complete)
        self.assertIn('fallback-parameter', result.issue_codes)
        self.assertEqual(result.segments[0].raw, b"utf-8''%E4%B8%AD%E6%96%87.txt")

    def test_continuation_beats_single_extended(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*=utf-8''wrong.txt;\n"
            " filename*0*=utf-8''%E6%96%87;\n"
            " filename*1*=.txt"
        )
        self.assertEqual(result.name, '文.txt' + INCOMPLETE_MARKER)
        self.assertFalse(result.complete)
        self.assertIn('conflicting-declaration', result.issue_codes)
        self.assertEqual(result.raw, b"utf-8''%E6%96%87.txt")

    def test_duplicate_plain_parameters_are_not_concatenated(self):
        result = info('Content-Disposition: attachment; filename=a.txt; filename=b.txt')
        self.assertNotEqual(result.name, 'a.txtb.txt')
        self.assertEqual(result.name, 'a.txt' + INCOMPLETE_MARKER)
        self.assertFalse(result.complete)
        self.assertIn('duplicate-parameter', result.issue_codes)

    def test_duplicate_extended_parameters_are_not_concatenated(self):
        result = info(
            "Content-Disposition: attachment; filename*=utf-8''a.txt; filename*=utf-8''b.txt"
        )
        self.assertEqual(result.name, 'a.txt' + INCOMPLETE_MARKER)
        self.assertFalse(result.complete)

    def test_repeated_identical_parameter_is_not_a_conflict(self):
        result = info('Content-Disposition: attachment; filename=a.txt; filename=a.txt')
        self.assertEqual(result.name, 'a.txt')
        self.assertTrue(result.complete)
        self.assertIn('repeated-parameter', result.issue_codes)

    def test_repeated_content_type_is_reported_but_not_fatal(self):
        result = info(
            'Content-Type: application/octet-stream\n'
            'Content-Type: application/octet-stream; name=x.txt\n'
            'Content-Disposition: attachment; filename=a.txt'
        )
        self.assertEqual(result.name, 'a.txt')
        self.assertTrue(result.complete)
        self.assertIn('repeated-header', result.issue_codes)

    def test_repeated_content_disposition_is_incomplete(self):
        part = part_from_headers(
            'Content-Disposition: attachment; filename=a.txt\n'
            'Content-Disposition: attachment; filename=a.txt'
        )
        result = build_filename_info(part)
        self.assertFalse(result.complete)
        self.assertIn('duplicate-header', result.issue_codes)
        self.assertTrue(result.name.endswith(INCOMPLETE_MARKER))

    def test_absurd_continuation_number_is_bounded(self):
        result = info(
            "Content-Disposition: attachment; filename*0*=utf-8''a; filename*999999999*=%41"
        )
        self.assertFalse(result.complete)
        self.assertIn('continuation-out-of-range', result.issue_codes)
        self.assertLess(len(result.name), 10)

    def test_conflicting_extended_and_continuation_forms_are_reported(self):
        result = info(
            "Content-Disposition: attachment; filename*=utf-8''a.txt; "
            "filename*0*=utf-8''b.txt"
        )
        self.assertEqual(result.name, 'b.txt' + INCOMPLETE_MARKER)
        self.assertFalse(result.complete)
        self.assertIn('conflicting-declaration', result.issue_codes)

    def test_agreeing_extended_and_continuation_forms_do_not_turn_fatal(self):
        result = info(
            "Content-Disposition: attachment; filename*=utf-8''a.txt; "
            "filename*0*=utf-8''a.txt"
        )
        self.assertEqual(result.name, 'a.txt')
        self.assertTrue(result.complete)
        self.assertIn('competing-declaration', result.issue_codes)


class FilenameContinuationTest(unittest.TestCase):
    def test_split_multibyte_character_spans_segments(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*0*=utf-8''%E4%B8;\n"
            " filename*1*=%AD.txt"
        )
        self.assertEqual(result.name, '中.txt')
        self.assertTrue(result.complete)
        self.assertEqual(result.raw, b"utf-8''%E4%B8%AD.txt")
        self.assertEqual([s.raw for s in result.segments], [b"utf-8''%E4%B8", b'%AD.txt'])
        self.assertEqual([s.data for s in result.segments], [b'\xe4\xb8', b'\xad.txt'])
        self.assertEqual(result.segments[0].charset, 'utf-8')
        self.assertEqual(result.segments[0].language, '')
        self.assertIsNone(result.segments[1].charset)

    def test_segments_are_ordered_by_number_not_by_position(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*1*=%AD.txt;\n"
            " filename*0*=utf-8''%E4%B8"
        )
        self.assertEqual(result.name, '中.txt')
        self.assertEqual([s.index for s in result.segments], [0, 1])

    def test_missing_segment_is_a_gap_not_a_complete_name(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*0*=utf-8''%E4%B8%AD;\n"
            " filename*2*=%E6%96%87.txt"
        )
        self.assertEqual(result.name, '中' + INCOMPLETE_MARKER + '文.txt')
        self.assertFalse(result.complete)
        self.assertIn('missing-segment', result.issue_codes)
        self.assertEqual([s.index for s in result.segments], [0, 2])

    def test_missing_leading_segment_is_a_gap(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*1*=%E6%96%87.txt"
        )
        self.assertTrue(result.name.startswith(INCOMPLETE_MARKER))
        self.assertIn('文.txt', result.name)
        self.assertFalse(result.complete)
        self.assertIn('missing-segment', result.issue_codes)

    def test_duplicate_number_never_produces_a_plain_name(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*0*=utf-8''%E4;\n"
            " filename*0*=%B8%AD.txt"
        )
        self.assertNotEqual(result.name, '中.txt')
        self.assertFalse(result.complete)
        self.assertIn('duplicate-segment', result.issue_codes)
        self.assertEqual(len([s for s in result.segments]), 1)

    def test_bytes_from_another_parameter_are_never_used(self):
        result = info(
            'Content-Type: application/octet-stream\n'
            'Content-Disposition: attachment;\n'
            " filename*0*=utf-8''%E4;\n"
            ' name*1*=%B8%AD.txt'
        )
        self.assertNotEqual(result.name, '中.txt')
        self.assertFalse(result.complete)
        self.assertEqual(result.raw, b"utf-8''%E4")
        self.assertEqual([s.raw for s in result.segments], [b"utf-8''%E4"])
        self.assertEqual([s.parameter for s in result.fallback_segments], ['name'])

    def test_multibyte_split_is_not_repaired_by_next_parameter(self):
        result = info(
            'Content-Type: application/octet-stream\n'
            'Content-Disposition: attachment;\n'
            " filename*0*=utf-8''%E4\n"
            ' name*1*=%B8%AD.txt'
        )
        self.assertFalse(result.complete)
        self.assertIn('decode-error', result.issue_codes)

    def test_conflicting_charset_on_later_segment(self):
        result = info(
            "Content-Disposition: attachment;\n"
            " filename*0*=utf-8''%E4%B8%AD;\n"
            " filename*1*=iso-8859-1''%AD.txt"
        )
        self.assertFalse(result.complete)
        self.assertIn('conflicting-declaration', result.issue_codes)
        self.assertEqual(result.charset, 'utf-8')

    def test_unknown_charset_does_not_raise(self):
        result = info("Content-Disposition: attachment; filename*=made-up''abc.txt")
        self.assertFalse(result.complete)
        self.assertIn('unknown-charset', result.issue_codes)
        self.assertIsInstance(result.name, str)

    def test_invalid_percent_escape_is_reported_and_kept(self):
        result = info("Content-Disposition: attachment; filename*=utf-8''abc%ZZ.txt")
        self.assertFalse(result.complete)
        self.assertIn('invalid-percent-escape', result.issue_codes)
        self.assertIn('%ZZ', result.name)

    def test_encoded_word_inside_plain_parameter(self):
        result = info(
            'Content-Disposition: attachment; filename="=?UTF-8?B?5Lit5paHLnR4dA==?="'
        )
        self.assertEqual(result.name, '中文.txt')
        self.assertTrue(result.complete)

    def test_charset_is_none_for_ascii_plain_value(self):
        result = info('Content-Disposition: attachment; filename="ascii.txt"')
        self.assertIsNone(result.charset)

    def test_declared_charset_is_adopted_even_with_bad_bytes(self):
        raw = (
            b'Content-Type: application/octet-stream; name*=iso-2022-jp\'ja\'Dij%8aat.mp3\n'
            b'Content-Disposition: attachment\n\nx'
        )
        part = email.message_from_bytes(raw)
        result = build_filename_info(part)
        self.assertEqual(result.name, 'Dij�at.mp3')
        self.assertEqual(result.charset, 'iso-2022-jp')
        self.assertFalse(result.complete)
        self.assertIn('decode-error', result.issue_codes)

    def test_legacy_replacement_is_marked_incomplete(self):
        message = MailMessage.from_bytes(
            b'Content-Type: application/octet-stream\n'
            b"Content-Disposition: attachment; filename*=utf-8''bad%FF.txt\n"
            b'\n'
            b'x'
        )
        attachment = message.attachments[0]
        self.assertFalse(attachment.filename_is_complete)
        self.assertIn('decode-error', attachment.filename_info.issue_codes)

    def test_nameless_part(self):
        part = part_from_headers('Content-Type: text/plain')
        result = build_filename_info(part)
        self.assertEqual(result.name, '')
        self.assertTrue(result.complete)
        self.assertIsNone(result.source)
        self.assertFalse(has_filename_parameter(part))

    def test_broken_parameter_still_marks_part_as_named(self):
        part = part_from_headers(
            "Content-Type: application/octet-stream\n"
            "Content-Disposition: attachment;\n"
            ' filename*0*=%E4%B8%AD'
        )
        self.assertTrue(has_filename_parameter(part))
        message = MailMessage.from_bytes(
            b"Content-Type: application/octet-stream\n"
            b"Content-Disposition: attachment;\n"
            b" filename*0*=%E4%B8%AD\n\npayload"
        )
        # The standard library ignores this parameter; the attachment must still appear.
        self.assertEqual(len(message.attachments), 1)
        attachment = message.attachments[0]
        self.assertIn('中', attachment.filename)
        self.assertIn('missing-charset', attachment.filename_info.issue_codes)
        self.assertIsInstance(attachment.filename, str)


class SafeFilenameTest(unittest.TestCase):
    def test_path_separators_and_parent_references_are_removed(self):
        self.assertNotIn('/', safe_filename('../../etc/passwd'))
        self.assertNotIn('\\', safe_filename(r'..\..\windows\system32\cfg'))
        self.assertNotIn(safe_filename('..'), ('..', '.', ''))

    def test_windows_reserved_names(self):
        self.assertEqual(safe_filename('CON.txt'), 'CON_.txt')
        self.assertEqual(safe_filename('lpt9'), 'lpt9_')

    def test_control_and_forbidden_characters(self):
        result = safe_filename('a\x00b\tc:d*e?f.txt')
        self.assertEqual(result, 'a_b c_d_e_f.txt')

    def test_empty_display_name_gets_fallback(self):
        self.assertEqual(safe_filename('', fallback='attachment'), 'attachment')
        self.assertEqual(safe_filename('...'), 'attachment')

    def test_long_name_keeps_extension(self):
        result = safe_filename('a' * 400 + '.pdf')
        self.assertLessEqual(len(result.encode()), 255)
        self.assertTrue(result.endswith('.pdf'))

    def test_keyboard_mash_stays_inside_one_component(self):
        for name in ('a/b', 'a\\b', 'a\x00b', '/tmp/x', 'C:\\temp\\x'):
            self.assertNotIn('/', safe_filename(name))

    def test_unique_filename_suffixes(self):
        taken = {'a.txt'}
        self.assertEqual(unique_filename('a.txt', taken.__contains__), 'a (1).txt')
        taken.add('a (1).txt')
        self.assertEqual(unique_filename('a.txt', taken.__contains__), 'a (2).txt')

    def test_unique_filename_keeps_extension_with_multiple_dots(self):
        self.assertEqual(unique_filename('a.tar.gz', {'a.tar.gz'}.__contains__), 'a.tar (1).gz')


class SaveAttachmentTest(unittest.TestCase):
    def test_two_legal_names_that_sanitize_alike_do_not_overwrite(self):
        message = MailMessage.from_bytes(
            b'Content-Type: multipart/mixed; boundary=b\n'
            b'\n'
            b'--b\n'
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="a/b.txt"\n'
            b'\n'
            b'first'
            b'\n--b\n'
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="a:b.txt"\n'
            b'\n'
            b'second'
            b'\n--b--\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = message.save_attachments(directory)
            self.assertEqual(len(paths), 2)
            self.assertNotEqual(paths[0], paths[1])
            self.assertEqual(paths[0].read_bytes(), b'first')
            self.assertEqual(paths[1].read_bytes(), b'second')
            self.assertEqual(paths[0].name, 'a_b.txt')
            self.assertEqual(paths[1].name, 'a_b (1).txt')

    def test_existing_file_is_not_replaced(self):
        message = MailMessage.from_bytes(
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="name.txt"\n'
            b'\n'
            b'new'
        )
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / 'name.txt'
            existing.write_bytes(b'old')
            saved = message.attachments[0].save(directory)
            self.assertEqual(existing.read_bytes(), b'old')
            self.assertEqual(saved.name, 'name (1).txt')
            self.assertEqual(saved.read_bytes(), b'new')

    def test_overwrite_is_explicit(self):
        message = MailMessage.from_bytes(
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="name.txt"\n'
            b'\n'
            b'new'
        )
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / 'name.txt'
            existing.write_bytes(b'old')
            saved = message.attachments[0].save(directory, overwrite=True)
            self.assertEqual(saved, existing)
            self.assertEqual(existing.read_bytes(), b'new')

    def test_display_name_is_unchanged(self):
        message = MailMessage.from_bytes(
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="../../passwd"\n'
            b'\n'
            b'x'
        )
        attachment = message.attachments[0]
        self.assertEqual(attachment.filename, '../../passwd')
        self.assertEqual(attachment.safe_filename, '_.._passwd')
        with tempfile.TemporaryDirectory() as directory:
            saved = attachment.save(directory)
            self.assertEqual(saved.parent, Path(directory))

    def test_manifest_records_names_and_raw_segments(self):
        import json
        message = MailMessage.from_bytes(
            b'Content-Type: multipart/mixed; boundary=b\n'
            b'\n'
            b'--b\n'
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment;\n'
            b" filename*0*=utf-8''%E4%B8;\n"
            b' filename*1*=%AD.txt\n'
            b'\n'
            b'first'
            b'\n--b\n'
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="a:b.txt"\n'
            b'\n'
            b'second'
            b'\n--b\n'
            b'Content-Type: application/octet-stream\n'
            b'Content-Disposition: attachment; filename="a?b.txt"\n'
            b'\n'
            b'third'
            b'\n--b--\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = message.save_attachments(directory, manifest=True)
            manifest_path = Path(directory) / 'attachments.json'
            self.assertTrue(manifest_path.exists())
            entries = json.loads(manifest_path.read_text(encoding='utf-8'))
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0]['display_name'], '中.txt')
            self.assertEqual(entries[0]['path'], str(paths[0]))
            self.assertTrue(entries[0]['complete'])
            self.assertEqual(
                [segment['raw_b64'] for segment in entries[0]['segments']],
                ['dXRmLTgnJyVFNCVCOA==', 'JUFELnR4dA=='],
            )
            self.assertEqual(entries[1]['display_name'], 'a:b.txt')
            self.assertEqual(entries[2]['display_name'], 'a?b.txt')
            self.assertEqual(paths[1].name, 'a_b.txt')
            self.assertEqual(paths[2].name, 'a_b (1).txt')
            before = sorted(path.name for path in Path(directory).iterdir())
            with self.assertRaises(FileExistsError):
                message.save_attachments(directory, manifest=True)
            after = sorted(path.name for path in Path(directory).iterdir())
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
