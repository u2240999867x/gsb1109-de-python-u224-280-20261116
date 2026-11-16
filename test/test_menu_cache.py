#!/usr/bin/env python3
"""Tests for desktop file ID merging, masking and rescanning in
xdg.Menu.MenuEntryCache.
"""
import os
import shutil
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

import xdg.Locale
from xdg.Menu import (MenuEntryCache, HIDDEN, SHADOWED, SCAN_INCOMPLETE)


def desktop_file(name, extra=""):
    return "[Desktop Entry]\nType=Application\nName=%s\nExec=true\n%s" % (name, extra)


class MenuEntryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dir_a = os.path.join(self.tmpdir, "a", "applications")
        self.dir_b = os.path.join(self.tmpdir, "b", "applications")
        os.makedirs(self.dir_a)
        os.makedirs(self.dir_b)
        self.cache = MenuEntryCache()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def write(self, dir_, relpath, content):
        path = os.path.join(dir_, relpath)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w") as f:
            f.write(content)
        return path

    def scan_ab(self):
        self.cache.add_menu_entries([self.dir_a, self.dir_b])

    def effective_by_id(self, dirs=None):
        entries = self.cache.get_menu_entries(dirs or [self.dir_a, self.dir_b])
        return dict((e.DesktopFileID, e) for e in entries)

    def test_priority_and_masked_report(self):
        self.write(self.dir_a, "foo.desktop", desktop_file("FooA"))
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))
        self.write(self.dir_b, "bar.desktop", desktop_file("BarB"))
        self.scan_ab()

        entries = self.effective_by_id()
        self.assertEqual(sorted(entries), ["bar.desktop", "foo.desktop"])
        self.assertEqual(entries["foo.desktop"].DesktopEntry.getName(), "FooA")

        masked = self.cache.get_masked_entries([self.dir_a, self.dir_b])
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0].MenuEntry.DesktopFileID, "foo.desktop")
        self.assertEqual(masked[0].MenuEntry.DesktopEntry.getName(), "FooB")
        self.assertEqual(masked[0].SourceDir, self.dir_b)
        self.assertEqual(masked[0].Reason, SHADOWED)

    def test_missing_file_in_upper_dir_falls_through(self):
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))
        self.scan_ab()

        entries = self.effective_by_id()
        self.assertEqual(entries["foo.desktop"].DesktopEntry.getName(), "FooB")
        self.assertEqual(self.cache.get_masked_entries([self.dir_a, self.dir_b]), [])

    def test_hidden_in_upper_dir_masks_lower(self):
        self.write(self.dir_a, "foo.desktop", desktop_file("FooA", "Hidden=true\n"))
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))
        self.scan_ab()

        entries = self.effective_by_id()
        # the lower-priority declaration must not surface
        self.assertEqual(list(entries), ["foo.desktop"])
        self.assertTrue(entries["foo.desktop"].DesktopEntry.getHidden())

        masked = self.cache.get_masked_entries([self.dir_a, self.dir_b])
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0].Reason, HIDDEN)
        self.assertEqual(masked[0].SourceDir, self.dir_b)

    def test_id_collision_keeps_all_originals(self):
        # "foo-bar.desktop" and "foo/bar.desktop" derive the same ID
        self.write(self.dir_a, "foo-bar.desktop", desktop_file("Flat"))
        self.write(self.dir_a, os.path.join("foo", "bar.desktop"), desktop_file("Nested"))
        self.cache.add_menu_entries([self.dir_a])

        groups = self.cache.get_entry_groups([self.dir_a])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.DesktopFileID, "foo-bar.desktop")
        # all originals are kept, not just the first one found
        self.assertEqual(len(group.Collisions), 2)
        names = set(e.DesktopEntry.getName() for e in group.Collisions)
        self.assertEqual(names, set(["Flat", "Nested"]))
        # the representative is deterministic, independent of traversal order
        self.assertEqual(group.Effective.Filename, "foo-bar.desktop")
        self.assertEqual(group.Effective.DesktopEntry.getName(), "Flat")

    def test_incomplete_scan_does_not_expose_lower(self):
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))

        real_listdir = os.listdir

        def failing_listdir(path):
            if os.path.normpath(path) == self.dir_a:
                raise OSError("permission denied")
            return real_listdir(path)

        with mock.patch("os.listdir", side_effect=failing_listdir):
            self.cache.add_menu_entries([self.dir_a, self.dir_b])

        self.assertFalse(self.cache.is_scan_complete(self.dir_a))
        self.assertTrue(self.cache.is_scan_complete(self.dir_b))

        # the lower-priority app must not be exposed: the upper directory
        # may contain a declaration for the same ID
        self.assertEqual(self.cache.get_menu_entries([self.dir_a, self.dir_b]), [])

        groups = self.cache.get_entry_groups([self.dir_a, self.dir_b])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0].Uncertain)
        self.assertIsNone(groups[0].Effective)
        self.assertEqual(groups[0].Masked[0].Reason, SCAN_INCOMPLETE)
        self.assertEqual(groups[0].Masked[0].SourceDir, self.dir_b)

    def test_missing_directory_is_confirmed_empty(self):
        missing = os.path.join(self.tmpdir, "missing", "applications")
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))
        self.cache.add_menu_entries([missing, self.dir_b])

        self.assertTrue(self.cache.is_scan_complete(missing))
        entries = self.cache.get_menu_entries([missing, self.dir_b])
        self.assertEqual([e.DesktopFileID for e in entries], ["foo.desktop"])

    def test_partial_rescan_updates_only_confirmed_scope(self):
        self.write(self.dir_a, "foo.desktop", desktop_file("FooA"))
        self.write(self.dir_b, "bar.desktop", desktop_file("BarB"))
        self.scan_ab()
        before = self.effective_by_id()
        bar_entry = before["bar.desktop"]

        # change and add files only in dir_a
        self.write(self.dir_a, "foo.desktop", desktop_file("FooA2"))
        self.write(self.dir_a, "baz.desktop", desktop_file("BazA"))
        self.cache.rescan_menu_entries([self.dir_a])

        after = self.effective_by_id()
        self.assertEqual(sorted(after), ["bar.desktop", "baz.desktop", "foo.desktop"])
        self.assertEqual(after["foo.desktop"].DesktopEntry.getName(), "FooA2")
        # the untouched directory's entries are not replaced
        self.assertIs(after["bar.desktop"], bar_entry)

    def test_directory_order_change_recomputes_masking(self):
        self.write(self.dir_a, "foo.desktop", desktop_file("FooA"))
        self.write(self.dir_b, "foo.desktop", desktop_file("FooB"))
        self.scan_ab()

        entries_ab = self.effective_by_id([self.dir_a, self.dir_b])
        self.assertEqual(entries_ab["foo.desktop"].DesktopEntry.getName(), "FooA")
        masked_ab = self.cache.get_masked_entries([self.dir_a, self.dir_b])
        self.assertEqual(masked_ab[0].SourceDir, self.dir_b)

        entries_ba = self.effective_by_id([self.dir_b, self.dir_a])
        self.assertEqual(entries_ba["foo.desktop"].DesktopEntry.getName(), "FooB")
        masked_ba = self.cache.get_masked_entries([self.dir_b, self.dir_a])
        self.assertEqual(masked_ba[0].SourceDir, self.dir_a)

    def test_actions_and_locale_from_selected_declaration(self):
        self.write(self.dir_a, "foo.desktop", desktop_file(
            "FooA", "Name[de]=GermanA\nActions=New;\n\n"
            "[Desktop Action New]\nName=New A\nExec=true --new\n"))
        self.write(self.dir_b, "foo.desktop", desktop_file(
            "FooB", "Name[de]=GermanB\nActions=Other;\n\n"
            "[Desktop Action Other]\nName=Other B\nExec=true --other\n"))
        self.scan_ab()

        entry = self.effective_by_id()["foo.desktop"]
        self.assertEqual(entry.DesktopEntry.getActions(), ["New"])

        old_langs = xdg.Locale.langs
        try:
            xdg.Locale.update("de")
            self.assertEqual(entry.DesktopEntry.getName(), "GermanA")
        finally:
            xdg.Locale.langs = old_langs


if __name__ == '__main__':
    unittest.main()
