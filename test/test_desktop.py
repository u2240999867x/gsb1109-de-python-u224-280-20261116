#!/usr/bin/env python3
# coding: utf-8
from xdg.DesktopEntry import DesktopEntry, _expand_exec
from xdg.Exceptions import (ValidationError, ParsingError, NoKeyError,
                            NoGroupError, ExecError)
from xdg.util import u

import resources

import io
import os
import shutil
import re
import tempfile
import unittest

class DesktopEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "gedit.desktop")
        with io.open(self.test_file, "w", encoding='utf-8') as f:
            f.write(resources.gedit_desktop)
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir)
    
    def test_write_file(self):
        de = DesktopEntry()
        de.parse(self.test_file)
        de.removeKey("Name")
        de.addGroup("Hallo")
        de.set("key", "value", "Hallo")
        
        new_file = os.path.join(self.tmpdir, "test.desktop")
        de.write(new_file, trusted=True)
        
        with io.open(new_file, encoding='utf-8') as f:
            contents = f.read()
        
        assert "[Hallo]" in contents, contents
        assert re.search(r"key\s*=\s*value", contents), contents
        
        # This is missing the Name key, and has an unknown Hallo group, so it
        # shouldn't validate.
        new_entry = DesktopEntry(new_file)
        self.assertRaises(ValidationError, new_entry.validate)
    
    def test_validate(self):
        entry = DesktopEntry(self.test_file)
        entry.validate()
    
    def test_values(self):
        entry = DesktopEntry(self.test_file)
        self.assertEqual(entry.getName(), 'gedit')
        self.assertEqual(entry.getGenericName(), 'Text Editor')
        self.assertEqual(entry.getNoDisplay(), False)
        self.assertEqual(entry.getComment(), 'Edit text files')
        self.assertEqual(entry.getIcon(), 'accessories-text-editor')
        self.assertEqual(entry.getHidden(), False)
        self.assertEqual(entry.getOnlyShowIn(), [])
        self.assertEqual(entry.getExec(), 'gedit %U')
        self.assertEqual(entry.getTerminal(), False)
        self.assertEqual(entry.getMimeTypes(), ['text/plain'])
        self.assertEqual(entry.getCategories(), ['GNOME', 'GTK', 'Utility', 'TextEditor'])
        self.assertEqual(entry.getTerminal(), False)
    
    def test_basic(self):
        entry = DesktopEntry(self.test_file)
        assert entry.hasKey("Categories")
        assert not entry.hasKey("TryExec")
        
        assert entry.hasGroup("Desktop Action Window")
        assert not entry.hasGroup("Desktop Action Door")
    
    def test_unicode_name(self):
        with io.open(self.test_file, "w", encoding='utf-8') as f:
            f.write(resources.unicode_desktop)
        
        entry = DesktopEntry(self.test_file)
        self.assertEqual(entry.getName(), u('Abc€þ'))
    
    def test_invalid(self):
        test_file = os.path.join(self.tmpdir, "spout.desktop")
        with io.open(test_file, "w", encoding='utf-8') as f:
            f.write(resources.spout_desktop)
        
        self.assertRaises(ParsingError, DesktopEntry, test_file)

    def test_invalid_unicode(self):
        test_file = os.path.join(self.tmpdir, "gnome-alsamixer.desktop")
        with io.open(test_file, "w", encoding='latin-1') as f:
            f.write(resources.gnome_alsamixer_desktop)
        
        # Just check this doesn't throw a UnicodeError.
        DesktopEntry(test_file)

class TestTryExec(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "foo.desktop")

    def test_present(self):
        with io.open(self.test_file, "w", encoding='utf-8') as f:
            f.write(resources.python_desktop)
        
        entry = DesktopEntry(self.test_file)
        res = entry.findTryExec()
        assert res, repr(res)

    def test_absent(self):
        with io.open(self.test_file, "w", encoding='utf-8') as f:
            f.write(resources.unicode_desktop)
        
        entry = DesktopEntry(self.test_file)
        res = entry.findTryExec()
        assert res is None, repr(res)
        
    def test_no_TryExec(self):
        with io.open(self.test_file, "w", encoding='utf-8') as f:
            f.write(resources.gedit_desktop)

        entry = DesktopEntry(self.test_file)
        self.assertRaises(NoKeyError, entry.findTryExec)


class TestExecExpansion(unittest.TestCase):
    """Tests for the pure Exec expansion helper."""

    def test_no_resources(self):
        self.assertEqual(_expand_exec("gedit"), [["gedit"]])
        self.assertEqual(_expand_exec("gedit %U"), [["gedit"]])
        self.assertEqual(_expand_exec("gedit %f"), [["gedit"]])
        # Resources are ignored when the Exec value has no resource code.
        self.assertEqual(_expand_exec("gedit", resources=["a", "b"]),
                         [["gedit"]])

    def test_multi_resource_codes_one_launch(self):
        self.assertEqual(_expand_exec("gedit %U", resources=["a", "b", "c"]),
                         [["gedit", "a", "b", "c"]])
        self.assertEqual(_expand_exec("gedit %F", resources=["a", "b"]),
                         [["gedit", "a", "b"]])
        # Multi code without resources contributes no arguments.
        self.assertEqual(_expand_exec("gedit %F"), [["gedit"]])

    def test_multi_resource_code_embedded_in_token(self):
        # Each resource is a separate element; surrounding text is flushed
        # into its own elements instead of being joined to the resources.
        self.assertEqual(_expand_exec("prefix%U", resources=["r1", "r2"]),
                         [["prefix", "r1", "r2"]])
        self.assertEqual(_expand_exec("%Usuffix", resources=["r1", "r2"]),
                         [["r1", "r2", "suffix"]])
        # A single-resource code substitutes inline within one element.
        self.assertEqual(_expand_exec("prefix%u", resources=["r"]),
                         [["prefixr"]])

    def test_single_resource_codes_one_launch_each(self):
        self.assertEqual(_expand_exec("gedit %u", resources=["a", "b"]),
                         [["gedit", "a"], ["gedit", "b"]])
        self.assertEqual(_expand_exec("gedit %f", resources=["a", "b", "c"]),
                         [["gedit", "a"], ["gedit", "b"], ["gedit", "c"]])

    def test_quoting_keeps_argv_structure(self):
        self.assertEqual(_expand_exec('"my program" --flag'),
                         [["my program", "--flag"]])
        self.assertEqual(_expand_exec('gedit "a b"'),
                         [["gedit", "a b"]])
        self.assertEqual(_expand_exec(r'gedit "a\"b"'),
                         [["gedit", 'a"b']])
        self.assertEqual(_expand_exec(r'echo "\$ \`"'),
                         [["echo", "$ `"]])
        self.assertEqual(_expand_exec("printf 100%%"),
                         [["printf", "100%"]])

    def test_icon_expands_to_two_arguments(self):
        self.assertEqual(_expand_exec("gedit %i", icon="my-icon"),
                         [["gedit", "--icon", "my-icon"]])
        # A missing icon must not produce empty business parameters.
        self.assertEqual(_expand_exec("gedit %i"), [["gedit"]])
        self.assertEqual(_expand_exec("gedit %i %u", icon="my-icon",
                                      resources=["x"]),
                         [["gedit", "--icon", "my-icon", "x"]])

    def test_display_name_with_spaces_is_one_value(self):
        self.assertEqual(_expand_exec("gedit %c", display_name="My Editor"),
                         [["gedit", "My Editor"]])
        self.assertEqual(_expand_exec("gedit %c"), [["gedit"]])

    def test_location(self):
        self.assertEqual(_expand_exec("gedit %k",
                                      location="/tmp/app.desktop"),
                         [["gedit", "/tmp/app.desktop"]])
        self.assertEqual(_expand_exec("gedit %k"), [["gedit"]])

    def test_resource_special_characters_are_verbatim(self):
        weird = '/tmp/a "quoted" 100% \\back'
        url = "https://example.com/a?x=100%20b&y=%2Fz"
        self.assertEqual(_expand_exec("gedit %U", resources=[weird]),
                         [["gedit", weird]])
        self.assertEqual(_expand_exec("gedit %f", resources=[weird, "b"]),
                         [["gedit", weird], ["gedit", "b"]])
        # Local files and URLs can be mixed; quoting, percent encoding and
        # backslashes in the resources are never read as Exec syntax.
        mixed = ["/tmp/a b.txt", url, 'weird"q%.desktop']
        self.assertEqual(_expand_exec("app %F", resources=mixed),
                         [["app"] + mixed])
        self.assertEqual(_expand_exec("app %f", resources=mixed),
                         [["app", item] for item in mixed])

    def test_quoted_field_code_is_literal(self):
        self.assertEqual(_expand_exec('gedit "%f"', resources=["x"]),
                         [["gedit", "%f"]])
        self.assertEqual(_expand_exec('gedit "100%"'),
                         [["gedit", "100%"]])

    def test_empty_tokens_are_dropped(self):
        # An empty quoted string and missing optional fields alike produce no
        # business parameters rather than empty argv elements.
        self.assertEqual(_expand_exec('gedit ""'), [["gedit"]])
        self.assertEqual(_expand_exec('a "" b'), [["a", "b"]])
        self.assertEqual(_expand_exec("gedit %i"), [["gedit"]])
        self.assertEqual(_expand_exec("gedit %c"), [["gedit"]])

    def test_repeated_field_codes_allowed(self):
        self.assertEqual(_expand_exec("g %u %u", resources=["a"]),
                         [["g", "a", "a"]])

    def test_incompatible_resource_codes_rejected(self):
        for bad in ("gedit %f %U", "gedit %F %u",
                    "gedit %f %F", "gedit %u %U"):
            self.assertRaises(ExecError, _expand_exec, bad,
                              resources=["a"])

    def test_malformed_exec_rejected(self):
        for bad in ('gedit %z', 'gedit %', 'gedit "unterminated',
                    'gedit \\', 'gedit \\x'):
            self.assertRaises(ExecError, _expand_exec, bad)


class TestDesktopEntryParseExec(unittest.TestCase):
    def _write_entry(self, name, contents):
        path = os.path.join(self.tmpdir, name)
        with io.open(path, "w", encoding='utf-8') as f:
            f.write(contents)
        return DesktopEntry(path)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.entry = self._write_entry("gedit.desktop", """\
[Desktop Entry]
Name=Text Editor
Icon=accessories-text-editor
Exec=gedit %U
Type=Application
Actions=Window;

[Desktop Action Window]
Name=Open a New Window
Exec=gedit --new-window %f
""")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_main_entry_multi_resources(self):
        self.assertEqual(self.entry.parseExec(["a", "b"]),
                         [["gedit", "a", "b"]])
        self.assertEqual(self.entry.parseExec(), [["gedit"]])

    def test_main_entry_icon_and_name(self):
        entry = self._write_entry("ic.desktop", """\
[Desktop Entry]
Name=My Nice Editor
Icon=my-icon
Exec=run %i %c
Type=Application
""")
        self.assertEqual(entry.parseExec(),
                         [["run", "--icon", "my-icon", "My Nice Editor"]])

    def test_action_exec_is_separately_expanded(self):
        self.assertEqual(self.entry.parseExec(["a"], action="Window"),
                         [["gedit", "--new-window", "a"]])
        self.assertEqual(
            self.entry.parseExec(["a", "b", "c"], action="Window"),
            [["gedit", "--new-window", "a"],
             ["gedit", "--new-window", "b"],
             ["gedit", "--new-window", "c"]])
        # The full group name is accepted as well.
        self.assertEqual(
            self.entry.parseExec(action="Desktop Action Window"),
            [["gedit", "--new-window"]])

    def test_original_exec_still_queryable(self):
        self.assertEqual(self.entry.getExec(), "gedit %U")
        self.entry.parseExec(["a", "b"])
        self.assertEqual(self.entry.getExec(), "gedit %U")
        self.assertEqual(self.entry.get("Exec", group="Desktop Action Window"),
                         "gedit --new-window %f")

    def test_unknown_action_raises(self):
        self.assertRaises(NoGroupError, self.entry.parseExec,
                          action="Missing")

    def test_missing_exec_raises(self):
        entry = self._write_entry("noexec.desktop", """\
[Desktop Entry]
Name=No Exec
Type=Application
""")
        self.assertRaises(NoKeyError, entry.parseExec)

    def test_illegal_combination_raises(self):
        entry = self._write_entry("bad.desktop", """\
[Desktop Entry]
Name=Bad
Type=Application
Exec=gedit %f %U
""")
        self.assertRaises(ExecError, entry.parseExec, ["a"])
