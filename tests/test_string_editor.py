import base64
import os
import struct
import tempfile
import unittest

import LnkParse3
from LnkParse3.string_editor import LnkEditError
from LnkParse3.string_editor import LnkStringEditor
from LnkParse3.string_editor import LnkStringLayout
from LnkParse3.string_editor import replace_strings
from LnkParse3.string_editor import rewrite
from LnkParse3.string_editor import UnsupportedCharacterError


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), 'samples')


def sample(name):
    with open(os.path.join(SAMPLE_DIR, name), 'rb') as fh:
        return base64.b64decode(fh.read())


def as_ansi(data, text_fields, cp='cp1252'):
    """Recode a Unicode sample in place so the IsUnicode flag is cleared."""
    editor = LnkStringEditor(data)
    values = {name: editor.value(name) for name in text_fields}
    layout = editor.layout

    out = bytearray(data[:76])
    flags = struct.unpack_from('<I', data, 20)[0] & ~0x80
    struct.pack_into('<I', out, 20, flags)
    out += data[76 : layout.string_data_start]
    for name in layout.order:
        encoded = values[name].encode(cp)
        out += struct.pack('<H', len(encoded)) + encoded
    out += data[layout.string_data_end :]
    return bytes(out)


class TestStringDataSemantics(unittest.TestCase):
    def test_counts_are_utf16_code_units_and_exclude_terminator(self):
        data = sample('microsoft_example')
        layout = LnkStringLayout(data)
        field = layout.fields['working_directory']
        self.assertEqual(field.count, 7)
        self.assertEqual(field.size, 2 + 7 * 2)
        self.assertEqual(
            data[field.payload_start : field.end], 'C:\\test'.encode('utf-16-le')
        )

    def test_empty_string_is_zero_count(self):
        data = sample('microsoft_example')
        editor = LnkStringEditor(data)
        editor.set('working_directory', '')
        result = editor.to_bytes()

        field = LnkStringLayout(result).fields['working_directory']
        self.assertEqual(field.count, 0)
        self.assertEqual(result[field.start : field.end], b'\x00\x00')
        self.assertEqual(
            LnkParse3.lnk_file(indata=result).string_data.working_directory(), ''
        )

    def test_ansi_count_is_encoded_bytes(self):
        data = as_ansi(
            sample('sample11'),
            ('relative_path', 'working_directory', 'command_line_arguments'),
        )
        layout = LnkStringLayout(data)
        self.assertFalse(layout.is_unicode)

        editor = LnkStringEditor(data, cp='cp1252')
        editor.set('working_directory', 'C:\\café')
        result = editor.to_bytes()

        field = LnkStringLayout(result).fields['working_directory']
        payload = 'C:\\café'.encode('cp1252')
        self.assertEqual(field.count, len(payload))
        self.assertEqual(result[field.payload_start : field.end], payload)

    def test_multibyte_ansi_code_page_counts_bytes(self):
        data = as_ansi(
            sample('sample11'),
            ('relative_path', 'working_directory', 'command_line_arguments'),
        )
        editor = LnkStringEditor(data, cp='cp932')
        editor.set('working_directory', 'C:\\日本')
        result = editor.to_bytes()

        field = LnkStringLayout(result).fields['working_directory']
        self.assertEqual(field.count, len('C:\\日本'.encode('cp932')))
        parsed = LnkParse3.lnk_file(indata=result, cp='cp932')
        self.assertEqual(parsed.string_data.working_directory(), 'C:\\日本')

    def test_allowed_code_page_list_picks_first_representable(self):
        data = as_ansi(
            sample('sample11'),
            ('relative_path', 'working_directory', 'command_line_arguments'),
        )
        editor = LnkStringEditor(data, cp=['cp1252', 'cp932'])
        editor.set('working_directory', 'C:\\日本')
        self.assertEqual(
            editor.staged_text('working_directory'), 'C:\\日本'
        )

    def test_surrogate_pair_maps_to_two_code_units(self):
        data = sample('microsoft_example')
        editor = LnkStringEditor(data)
        editor.set('working_directory', 'C:\\😀')
        result = editor.to_bytes()

        field = LnkStringLayout(result).fields['working_directory']
        self.assertEqual(field.count, len('C:\\😀'.encode('utf-16-le')) // 2)


class TestRewriteFidelity(unittest.TestCase):
    def test_unchanged_fields_and_extra_bytes_are_preserved(self):
        for name in ('microsoft_example', 'sample11', 'sample12', 'unknown_block'):
            with self.subTest(sample=name):
                data = sample(name)
                editor = LnkStringEditor(data)
                target = (
                    'working_directory'
                    if 'working_directory' in editor.fields()
                    else editor.fields()[0]
                )
                before = {
                    field: editor.value(field)
                    for field in editor.fields()
                    if field != target
                }
                layout = editor.layout
                tail = data[layout.extra_data_start :]

                editor.set(target, editor.value(target) + '!')
                result = editor.to_bytes()

                parsed = LnkParse3.lnk_file(indata=result)
                self.assertEqual(parsed.size, len(result))
                for field, value in before.items():
                    self.assertEqual(getattr(parsed.string_data, field)(), value)
                self.assertEqual(
                    result[LnkStringLayout(result).extra_data_start :], tail
                )

    def test_growing_and_shrinking_moves_later_data_exactly(self):
        data = sample('microsoft_example')
        original_size = len(data)
        for text in ('C:\\', 'C:\\' + 'a' * 100):
            with self.subTest(length=len(text)):
                editor = LnkStringEditor(data)
                old = editor.value('working_directory')
                editor.set('working_directory', text)
                result = editor.to_bytes()

                expected_delta = (len(text) - len(old)) * 2
                self.assertEqual(len(result), original_size + expected_delta)

                layout = LnkStringLayout(result)
                self.assertEqual(
                    layout.extra_data_start,
                    editor.layout.extra_data_start + expected_delta,
                )
                self.assertEqual(
                    result[layout.extra_data_start :],
                    data[editor.layout.extra_data_start :],
                )
                self.assertEqual(
                    LnkParse3.lnk_file(indata=result).string_data.working_directory(),
                    text,
                )

    def test_original_bytes_are_not_modified(self):
        data = sample('microsoft_example')
        snapshot = bytes(data)
        editor = LnkStringEditor(data)
        editor.set('working_directory', 'C:\\changed')
        editor.to_bytes()
        self.assertEqual(data, snapshot)

    def test_identity_copy_is_byte_identical(self):
        data = sample('microsoft_example')
        self.assertEqual(LnkStringEditor(data).to_bytes(), data)

    def test_same_value_keeps_terminated_layout(self):
        data = sample('microsoft_example')
        editor = LnkStringEditor(data)
        value = editor.value('working_directory')
        editor.set('working_directory', value)
        self.assertEqual(editor.to_bytes(), data)


class TestRejections(unittest.TestCase):
    def test_declared_length_running_into_next_field_is_rejected(self):
        # working_directory declares 1693 characters although the real field is
        # 260 characters; the next field is then read from the wrong offset.
        with self.assertRaises(LnkEditError):
            LnkStringEditor(sample('padded_cli_arguments'))

    def test_incomplete_extra_data_is_rejected(self):
        # The last 4 bytes only look like a block size; the block itself is absent.
        with self.assertRaises(LnkEditError):
            LnkStringEditor(sample('extra_data'))

    def test_ambiguous_link_info_flags_are_rejected(self):
        data = bytearray(sample('sample11'))
        flags = struct.unpack_from('<I', data, 20)[0]
        struct.pack_into('<I', data, 20, flags | 0x100)
        with self.assertRaises(LnkEditError):
            LnkStringEditor(bytes(data))

    def test_embedded_nul_is_rejected(self):
        editor = LnkStringEditor(sample('microsoft_example'))
        with self.assertRaises(LnkEditError):
            editor.set('working_directory', 'C:\\te\x00st')

    def test_lone_surrogate_is_rejected(self):
        editor = LnkStringEditor(sample('microsoft_example'))
        with self.assertRaises(UnsupportedCharacterError):
            editor.set('working_directory', '\ud800')

    def test_unrepresentable_character_policy(self):
        data = as_ansi(
            sample('sample11'),
            ('relative_path', 'working_directory', 'command_line_arguments'),
        )
        with self.assertRaises(UnsupportedCharacterError):
            LnkStringEditor(data).set('working_directory', 'C:\\новый')

        for policy in ('replace', 'ignore', 'xmlcharrefreplace', 'backslashreplace'):
            with self.subTest(policy=policy):
                editor = LnkStringEditor(data, unrepresentable=policy)
                editor.set('working_directory', 'C:\\новый')
                result = editor.to_bytes()
                self.assertEqual(
                    LnkParse3.lnk_file(indata=result).size, len(result)
                )

    def test_too_long_value_is_rejected(self):
        editor = LnkStringEditor(sample('microsoft_example'))
        with self.assertRaises(LnkEditError):
            editor.set('working_directory', 'a' * 261)
        with self.assertRaises(LnkEditError):
            editor.set('command_line_arguments', 'a' * 65536)


class TestOutputFiles(unittest.TestCase):
    def test_write_creates_copy_and_leaves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'source.lnk')
            destination = os.path.join(tmp, 'copy.lnk')
            data = sample('microsoft_example')
            with open(source, 'wb') as fh:
                fh.write(data)

            from LnkParse3.string_editor import load

            editor = load(source, cp='cp1252')
            editor.set('working_directory', 'C:\\copy')
            editor.write(destination)

            with open(source, 'rb') as fh:
                self.assertEqual(fh.read(), data)
            with open(destination, 'rb') as fh:
                copy = fh.read()
            self.assertEqual(
                LnkParse3.lnk_file(indata=copy).string_data.working_directory(),
                'C:\\copy',
            )

    def test_refuses_to_overwrite_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'source.lnk')
            with open(source, 'wb') as fh:
                fh.write(sample('microsoft_example'))

            from LnkParse3.string_editor import load

            editor = load(source)
            editor.set('working_directory', 'C:\\copy')
            with self.assertRaises(LnkEditError):
                editor.write(source)


class TestConvenienceApi(unittest.TestCase):
    def test_replace_strings(self):
        data = sample('microsoft_example')
        result = replace_strings(data, {'working_directory': 'D:\\work'})
        parsed = LnkParse3.lnk_file(indata=result)
        self.assertEqual(parsed.string_data.working_directory(), 'D:\\work')
        self.assertEqual(parsed.string_data.relative_path(), '.\\a.txt')

    def test_rewrite_with_keyword_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = os.path.join(tmp, 'copy.lnk')
            rewrite(
                sample('microsoft_example'),
                destination,
                working_directory='D:\\work',
                relative_path='.\\b.txt',
            )
            with open(destination, 'rb') as fh:
                parsed = LnkParse3.lnk_file(indata=fh.read())
            self.assertEqual(parsed.string_data.working_directory(), 'D:\\work')
            self.assertEqual(parsed.string_data.relative_path(), '.\\b.txt')

    def test_missing_field_is_rejected(self):
        data = sample('microsoft_example')
        with self.assertRaises(LnkEditError):
            replace_strings(data, {'command_line_arguments': '/q'})

    def test_lnk_file_editor(self):
        data = sample('microsoft_example')
        parsed = LnkParse3.lnk_file(indata=data)
        editor = parsed.string_editor()
        editor.set('working_directory', 'D:\\work')
        result = editor.to_bytes()
        self.assertEqual(
            LnkParse3.lnk_file(indata=result).string_data.working_directory(),
            'D:\\work',
        )


if __name__ == '__main__':
    unittest.main()
